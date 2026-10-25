# -*- coding: utf-8 -*-
"""CLI 入口：扫描 → 审阅清单 → 勾选确认 → 清理（支持多盘 C/D/E/F）。

用法：
  python -m diskcleaner.cli drives                 # 列出本机所有盘
  python -m diskcleaner.cli scan C: D: E: F:\\     # 多盘扫描出报告（不删）
  python -m diskcleaner.cli scan --all-drives      # 扫所有本地磁盘
  python -m diskcleaner.cli clean C:               # 只列清单，不动手
  python -m diskcleaner.cli clean C: --safe        # 清 safe 项（先预览再加 --execute）
  python -m diskcleaner.cli clean C: --safe --execute     # 执行（safe 项进回收站）
  python -m diskcleaner.cli clean C: --paths "C:\\a\\b" "C:\\c" --execute
  python -m diskcleaner.cli rules                  # 列出所有规则
  python -m diskcleaner.cli big F:\\               # 仅看大目录/大文件

安全原则：清单只做参考，删除范围必须显式指定（--paths/--safe/--include-review/--all）。
"""
from __future__ import annotations
import os
import sys
import re
import json
import shutil
import string
import ctypes
import argparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .scanner import scan, targeted_scan, top_dirs, top_files, human
from .analyzer import analyze, summarize, Candidate
from .cleaner import clean, empty_recycle_bin
from . import perm
from .rules import (ALL_RULES, CATEGORY_NAMES, RISK_NAMES,
                    rules_for_drive, drive_of, expand_placeholders)


def _path_size(p: str) -> int:
    """任意路径的大小（文件=自身，目录=递归和）。"""
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    total = 0
    for r, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def _eprint(*a):
    print(*a, file=sys.stderr)


# ============================================================
# 盘符工具
# ============================================================
_DRIVE_TYPES = {0: "未知", 1: "无根", 2: "可移动", 3: "本地磁盘",
                4: "网络", 5: "光驱", 6: "RAM盘"}


def list_drives() -> list:
    """枚举本机所有盘符，返回 [{drive, type, type_name, label, total, free, used, used_pct}]。"""
    out = []
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    for d in string.ascii_uppercase:
        root = d + ":" + os.sep
        if not os.path.exists(root):
            continue
        dtype, label = 3, ""
        if kernel32:
            try:
                dtype = kernel32.GetDriveTypeW(root)
            except Exception:
                dtype = 3
            try:
                vol = ctypes.create_unicode_buffer(261)
                kernel32.GetVolumeInformationW(root, vol, 261, None, None, None, None, 0)
                label = vol.value or ""
            except Exception:
                label = ""
        try:
            u = shutil.disk_usage(root)
            out.append({"drive": root, "type": dtype,
                        "type_name": _DRIVE_TYPES.get(dtype, str(dtype)),
                        "label": label, "total": u.total, "free": u.free,
                        "used": u.used,
                        "used_pct": round(u.used / u.total * 100) if u.total else 0})
        except Exception:
            out.append({"drive": root, "type": dtype,
                        "type_name": _DRIVE_TYPES.get(dtype, str(dtype)),
                        "label": label, "total": 0, "free": 0, "used": 0, "used_pct": 0})
    return out


def _normalize_root(arg: str) -> str:
    """把 'C:' / 'c' / 'D:\\' / 'D:/x' 规整成绝对根路径。"""
    a = arg.strip().rstrip("/\\")
    # 单字母 -> 盘符根
    if len(a) == 1 and a.isalpha():
        return a.upper() + ":" + os.sep
    # 字母+冒号（rstrip 已去掉尾部斜杠）-> 盘符根。注意 a 此时是 'C:'，只取首字母再补冒号
    if len(a) == 2 and a[0].isalpha() and a[1] == ":":
        return a[0].upper() + ":" + os.sep
    return os.path.abspath(arg)


def _resolve_roots(args) -> list:
    """解析 --all-drives / 多盘参数，返回去重的根路径列表。"""
    roots = []
    if getattr(args, "all_drives", False):
        roots = [d["drive"] for d in list_drives() if d["type"] == 3]  # 仅本地磁盘
    for r in (args.root or []):
        roots.append(_normalize_root(r))
    seen, uniq = set(), []
    for r in roots:
        k = os.path.normcase(os.path.abspath(r))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def _scan_and_analyze(roots: list, cache: bool, dirs_only: bool, deep: bool = False):
    """逐个盘扫描+分析，合并候选。返回 (all_cands, scan_meta)。"""
    all_cands = []
    meta = []
    multi = len(roots) > 1
    admin = perm.is_admin()
    locked_all = {"count": 0, "size": 0}
    for root in roots:
        if not os.path.isdir(root):
            _eprint(f"[!] 跳过（不存在）: {root}")
            continue
        drv = drive_of(root).upper() or "?"
        is_drive_root = bool(re.match(r"^[a-zA-Z]:[\\/]?$", root.strip()))
        if is_drive_root and not deep:
            _eprint(f"[*] 靶向快扫 {drv} 盘 ({root}) ...")
            res = targeted_scan(root,
                       progress=lambda d, n, _d=drv: _eprint(
                           f"\r    {_d}: {n} 文件 | {d[:52]}") if n % 1000 == 0 else None)
        else:
            _eprint(f"[*] 全盘深扫 {drv} 盘 ({root}) ...")
            res = scan(root, use_cache=cache,
                       progress=lambda d, n, _d=drv: _eprint(
                           f"\r    {_d}: {n} 文件 | {d[:52]}") if n % 5000 == 0 else None)
        _eprint(f"\r[✓] {drv} 盘: {human(res.total_size)}, {res.nfiles} 文件, "
                f"{res.ndirs} 目录, {res.elapsed:.1f}s, {len(res.errors)} 错误")
        locked = {"count": 0, "size": 0}
        cands = analyze(res, include_files=not dirs_only, admin=admin,
                        locked_stats=locked)
        locked_all["count"] += locked["count"]
        locked_all["size"] += locked["size"]
        if multi:
            for c in cands:
                c.title = f"[{drv}] {c.title}"   # 多盘时标盘符
        all_cands.extend(cands)
        summ = summarize(cands)
        meta.append({"drive": drv, "root": root, "total": res.total_size,
                     "total_h": human(res.total_size), "nfiles": res.nfiles,
                     "elapsed": round(res.elapsed, 1),
                     "cand_count": summ["count"], "cand_size": summ["total_size"],
                     "cand_size_h": summ["total_size_h"],
                     "selected_h": summ["total_selected_h"],
                     "errors": res.errors[:10]})
    risk_order = {"safe": 0, "review": 1, "caution": 2}
    all_cands.sort(key=lambda c: (risk_order.get(c.risk, 1), -c.size))
    # 权限提示：非管理员滤掉的系统级垃圾
    if not admin and locked_all["count"]:
        _eprint(f"\n[i] 非管理员运行：另有 {locked_all['count']} 项系统垃圾"
                f"（{human(locked_all['size'])}）需要管理员权限，本次未列入。"
                f"以管理员身份重开可多清。")
    return all_cands, meta


# ============================================================
# 命令
# ============================================================
def cmd_drives(args):
    drives = list_drives()
    print(f"=== 本机磁盘 ({len(drives)} 个) ===\n")
    print(f"{'盘符':<5}{'类型':<9}{'卷标':<13}{'总量':>7}{'可用':>7}{'已用':>6}  建议")
    print("-" * 66)
    for d in drives:
        tot = f"{d['total']/2**30:.0f}G" if d["total"] else "?"
        free = f"{d['free']/2**30:.0f}G" if d["total"] else "?"
        adv = ""
        if d["type"] == 3 and d["total"]:
            if d["used_pct"] >= 85:
                adv = "🔴 很满，建议清"
            elif d["used_pct"] >= 70:
                adv = "🟡 偏满，可清"
            else:
                adv = "🟢 充足"
        print(f"{d['drive']:<5}{d['type_name']:<8}{(d['label'] or '(无)'):<13}"
              f"{tot:>7}{free:>7}{str(d['used_pct'])+'%':>6}  {adv}")
    print(f"\n系统盘: {os.environ.get('SystemDrive', 'C:')}    "
          f"用户目录: {os.path.expanduser('~')}")
    print("\n扫描示例: python -m diskcleaner.cli scan C: D: E: F:\\")


def cmd_scan(args):
    roots = _resolve_roots(args)
    if not roots:
        _eprint("[!] 未指定扫描盘/目录。用 `drives` 看有哪些盘。")
        return
    all_cands, meta = _scan_and_analyze(roots, args.cache, args.dirs_only, deep=args.deep)
    summ = summarize(all_cands)

    if len(meta) > 1:
        print("\n=== 各盘概览 ===")
        for m in meta:
            print(f"  {m['drive']}盘: 占用 {m['total_h']:>8} | 可清理 "
                  f"{m['cand_count']} 项 {m['cand_size_h']}")

    print(f"\n=== 合计可清理候选: {summ['count']} 项, 共 {summ['total_size_h']} ===")
    print(f"    （清单仅供参考，勾选确认后才会清理）\n")

    print("--- 按风险 ---")
    for k, v in summ["by_risk"].items():
        print(f"  {k}: {v['count']} 项, {v['size_h']}")
    print("\n--- 按类别 ---")
    for k, v in summ["by_category"].items():
        print(f"  {k}: {v['count']} 项, {v['size_h']}")

    print(f"\n--- 候选明细 (前 {args.limit or 60}) ---")
    for c in all_cands[:args.limit or 60]:
        sel = "[x]" if c.selected else "[ ]"
        gp = f" ×{c.group_count}" if getattr(c, "is_group", False) else ""
        print(f"  {sel} {c.risk:7} {human(c.size):>10}  {c.title}{gp}  {c.path}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summ, "drives": meta,
                       "candidates": [c.to_dict() for c in all_cands]},
                      f, ensure_ascii=False, indent=2)
        _eprint(f"\n[✓] JSON 已写入 {args.json}")


def cmd_clean(args):
    roots = _resolve_roots(args)
    if not roots:
        _eprint("[!] 未指定清理盘/目录。")
        return
    all_cands, meta = _scan_and_analyze(roots, args.cache, args.dirs_only,
                                        deep=getattr(args, "deep", False))

    # 选择范围必须显式声明（教训：以前 safe 默认全选，--execute 一跑就全清）。
    # 不给任何选择标志 = 只看清单，不动手。
    for c in all_cands:
        if args.paths:
            # 显式点名模式：只选用户列出的路径
            c.selected = False
        elif args.all:
            c.selected = True
        elif args.include_review:
            c.selected = c.risk in ("safe", "review")
        elif args.safe:
            c.selected = c.risk == "safe"
        # 否则全部保持未选中

    # 命令行指定路径：用户显式点名，优先于规则筛选
    if args.paths:
        sel_set = {os.path.normcase(os.path.abspath(p)) for p in args.paths}
        matched = set()
        for c in all_cands:
            key = os.path.normcase(os.path.abspath(c.path))
            c.selected = key in sel_set
            if c.selected:
                matched.add(key)
        for p in args.paths:
            ap = os.path.abspath(p)
            key = os.path.normcase(ap)
            if key in matched or not os.path.exists(ap):
                continue
            is_dir = os.path.isdir(ap)
            try:
                mt = os.path.getmtime(ap)
            except OSError:
                mt = 0.0
            all_cands.append(Candidate(
                path=ap, size=_path_size(ap), is_dir=is_dir, mtime=mt,
                rule_id="manual", category="other", risk="review",
                title="用户指定", description="命令行显式指定路径",
                selected=True))

    selected = [c for c in all_cands if c.selected]
    if not selected:
        _eprint("[!] 没有选中任何项。清理范围必须显式指定：")
        _eprint("    --paths <路径...>   只清指定路径（推荐）")
        _eprint("    --safe              清全部 safe 项")
        _eprint("    --include-review    清 safe + review 项")
        _eprint("    --all               清全部风险项")
        return

    total = sum(c.size for c in selected)
    mode = "DRY-RUN 预览" if args.dry_run else ("回收站" if not args.purge else "永久删除")
    _eprint(f"\n[*] 将清理 {len(selected)} 项, 共 {human(total)} [{mode}]\n")

    log_dir = args.log_dir or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "diskcleaner", "logs")
    report = clean(selected, dry_run=args.dry_run, use_trash=not args.purge,
                   log_dir=log_dir,
                   progress=lambda i, t, p: _eprint(f"\r    [{i+1}/{t}] {p[:50]}"))
    _eprint("")
    print(report.summary())
    if report.log_file:
        _eprint(f"\n[✓] 日志: {report.log_file}")
    if getattr(args, "empty_recycle_bin", False) and not args.dry_run:
        _eprint("\n[*] 正在自动清空回收站以释放物理空间 ...")
        if empty_recycle_bin():
            _eprint("[✓] 回收站已清空，物理磁盘空间已释放。")
        else:
            _eprint("[!] 回收站清空失败。")


def cmd_empty_recycle_bin(args):
    drive = args.drive
    dl = None
    if drive:
        dl = drive_of(_normalize_root(drive)).upper() + ":\\"
    _eprint(f"[*] 正在静默清空 Windows 回收站{f' ({dl})' if dl else ''} ...")
    ok = empty_recycle_bin(dl)
    if ok:
        print("[✓] 回收站已成功清空！物理磁盘空间已释放。")
    else:
        print("[!] 清空回收站失败或发生异常。")


def cmd_rules(args):
    drive = args.drive
    if drive:
        dl = drive_of(_normalize_root(drive))
        rules = rules_for_drive(dl)
        title = f"适用于 {dl.upper()} 盘的规则 ({len(rules)} 条)"
    else:
        rules = ALL_RULES
        title = f"规则库 ({len(rules)} 条)"
    print(f"=== {title} ===\n")
    by_cat: dict = {}
    for r in rules:
        by_cat.setdefault(r.category, []).append(r)
    for cat, rs in by_cat.items():
        print(f"[{CATEGORY_NAMES.get(cat, cat)}]")
        for r in rs:
            age = f" (>{r.min_age_days}天)" if r.min_age_days else ""
            drv = r.drive or "任意盘"
            print(f"  {r.risk:7} {r.id:22} {r.title}{age}  [{drv}]")
            print(f"          {expand_placeholders(r.pattern)}")
        print()


def cmd_big(args):
    roots = _resolve_roots(args)
    for root in roots:
        if not os.path.isdir(root):
            _eprint(f"[!] 跳过（不存在）: {root}")
            continue
        drv = drive_of(root).upper() or root
        _eprint(f"[*] 扫描 {drv} ...")
        res = scan(root, use_cache=args.cache)
        _eprint(f"[✓] {drv}: {human(res.total_size)}, {res.nfiles} 文件")

        print(f"\n=== {drv} 最大目录 (前 {args.n}) ===")
        for size, p in top_dirs(res, n=args.n, min_size=args.min_mb * 2**20):
            print(f"  {human(size):>10}  {p}")

        print(f"\n=== {drv} 最大文件 (前 {args.n}) ===")
        for size, p in top_files(root, n=args.n, min_size=args.min_mb * 2**20):
            print(f"  {human(size):>10}  {p}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="diskcleaner", description="磁盘扫描 + 智能分类 + 安全清理（支持多盘）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # drives
    p_drv = sub.add_parser("drives", help="列出本机所有磁盘")
    p_drv.set_defaults(func=cmd_drives)

    # scan（多盘）
    p_scan = sub.add_parser("scan", help="扫描并生成可清理报告（不删除）")
    p_scan.add_argument("root", nargs="*", help="盘符或目录，可多个，如 C: D: E:")
    p_scan.add_argument("--all-drives", action="store_true", help="扫描所有本地磁盘")
    p_scan.add_argument("--deep", action="store_true", help="全盘深度遍历（默认执行靶向快扫）")
    p_scan.add_argument("--cache", action="store_true", help="使用缓存（1小时内）")
    p_scan.add_argument("--dirs-only", action="store_true", help="只分析目录，跳过单文件")
    p_scan.add_argument("--limit", type=int, default=60, help="明细显示条数")
    p_scan.add_argument("--json", help="输出 JSON 到文件")
    p_scan.set_defaults(func=cmd_scan)

    # clean（多盘）
    p_clean = sub.add_parser("clean", help="清理（默认只列清单不动手）")
    p_clean.add_argument("root", nargs="*", help="盘符或目录，可多个")
    p_clean.add_argument("--all-drives", action="store_true", help="所有本地磁盘")
    p_clean.add_argument("--deep", action="store_true", help="全盘深度遍历（默认执行靶向快扫）")
    p_clean.add_argument("--dry-run", action="store_true", default=True, help="预览模式（默认）")
    p_clean.add_argument("--execute", dest="dry_run", action="store_false", help="实际执行删除（进回收站）")
    p_clean.add_argument("--empty-recycle-bin", action="store_true", help="清理完成后顺便清空回收站以释放物理空间")
    p_clean.add_argument("--purge", action="store_true", help="永久删除（不进回收站，慎用）")
    p_clean.add_argument("--safe", action="store_true", help="选择全部 safe 项")
    p_clean.add_argument("--include-review", action="store_true", help="选择 safe + review 风险项")
    p_clean.add_argument("--all", action="store_true", help="选择所有风险项")
    p_clean.add_argument("--paths", nargs="*", help="只清理指定路径（最稳妥）")
    p_clean.add_argument("--cache", action="store_true", help="使用扫描缓存")
    p_clean.add_argument("--dirs-only", action="store_true")
    p_clean.add_argument("--log-dir", help="日志目录")
    p_clean.set_defaults(func=cmd_clean)

    # empty-recycle-bin
    p_erb = sub.add_parser("empty-recycle-bin", help="静默清空 Windows 回收站")
    p_erb.add_argument("drive", nargs="?", help="可选盘符，如 C:，不填则清空所有盘")
    p_erb.set_defaults(func=cmd_empty_recycle_bin)

    # rules
    p_rules = sub.add_parser("rules", help="列出规则库")
    p_rules.add_argument("drive", nargs="?", help="可选盘符，只看适用该盘的规则，如 D:")
    p_rules.set_defaults(func=cmd_rules)

    # big（多盘）
    p_big = sub.add_parser("big", help="仅看大目录/大文件")
    p_big.add_argument("root", nargs="*", help="盘符或目录，可多个")
    p_big.add_argument("--all-drives", action="store_true", help="所有本地磁盘")
    p_big.add_argument("--n", type=int, default=40)
    p_big.add_argument("--min-mb", type=int, default=50)
    p_big.add_argument("--cache", action="store_true")
    p_big.set_defaults(func=cmd_big)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
