# -*- coding: utf-8 -*-
"""winapp2.ini 兼容解析器：白嫖社区规则库（约 4000 应用条目），不必自己维护。

winapp2.ini 是 CCleaner 生态的事实标准（github.com/bleachbit/winapp2.ini，
1.8MB，2026-08 仍在更新）。格式：
    [App Name *]                  节名，`*` 后缀 = 默认不启用（CCleaner 语义）
    Detect=HKCU\\Software\\Key     注册表键存在才启用
    DetectFile=%AppData%\\App     文件/目录存在才启用
    SpecialDetect=DET_CHROME      特殊检测（本版忽略——路径不存在自然无候选）
    DetectOS=2600|3000            版本范围（本版忽略）
    Default=False
    Warning=删了会……
    FileKey1=%AppData%\\App\\Cache|*.tmp;*.log|RECURSE
    RegKey1=HKCU\\...|Value       注册表清理（本版不支持，跳过并计数）
    ExcludeKey1=%AppData%\\App\\Cache\\keep|*.*

映射到本项目 Rule 的策略（保守）：
- 所有 INI 规则一律 risk="review"（社区规则不背书 safe；勾选 = 用户确认）
- REMOVESELF（整目录删）→ risk="caution"
- 路径展开后在 Windows/Program Files/ProgramData 下 → needs_admin=True
- ExcludeKey → Rule.exclude_patterns（子树整体排除，宁可少删不错删）
- Detect/DetectFile 在解析期检查（便宜），不存在的节直接丢弃——
  几千条规则里大部分应用本机没装，这样剩下的才是有效规则

用法：
    from diskcleaner.winapp2 import load_winapp2
    extra_rules = load_winapp2(r"D:\\rules\\winapp2.ini")
    cands = analyze(res, rules=ALL_RULES + extra_rules)
"""
from __future__ import annotations
import io
import os
import re
import sys
from .rules import Rule, expand_placeholders, norm

# winapp2 环境变量 → Windows 实际环境变量（大小写不敏感）
_ENV_MAP = {
    "appdata": "APPDATA",
    "localappdata": "LOCALAPPDATA",
    "commonappdata": "ProgramData",
    "programfiles": "ProgramFiles",
    "programfilescommon": "ProgramFiles(x86)",  # 近似；常见垃圾不在意 x86 差异
    "homedrive": "SystemDrive",
    "homedir": "USERPROFILE",
    "system": "SystemRoot",       # %System% = Windows\System32
    "systemdrive": "SystemDrive",
    "windir": "SystemRoot",
    "userprofile": "USERPROFILE",
    "userprofiles": "USERPROFILE",
    "docs": "USERPROFILE",        # 谨慎：文档目录的清理规则展开到用户根下
    "music": "USERPROFILE",
    "pictures": "USERPROFILE",
    "video": "USERPROFILE",
    "documentsettings": "USERPROFILE",  # XP 遗留，本机路径多半不存在，自然失效
}

_REG_ROOTS = {
    "HKCU": 0x80000001,  # HKEY_CURRENT_USER
    "HKLM": 0x80000002,
    "HKCR": 0x80000000,
    "HKU":  0x80000003,
}

_LANGSEFREF_CAT = {
    "3022": "browser", "3030": "browser",
    "3024": "system", "3025": "system",
}

_ADMIN_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
stats = {"sections": 0, "rules": 0, "skipped_regkey": 0, "skipped_detect": 0,
         "skipped_removeself": 0}


def _expand_winapp2_path(p: str) -> str:
    """winapp2 变量 → 具体路径（norm 形态 c:/...）。"""
    def repl(m):
        name = m.group(1).strip().lower()
        env = _ENV_MAP.get(name, name.upper())
        return os.environ.get(env, m.group(0))
    p = re.sub(r"%([^%]+)%", repl, p)
    return norm(p)


def _detect_ok_registry(key_path: str) -> bool:
    """Detect=HKCU\\Software\\... 键是否存在。"""
    m = re.match(r"^(HK[a-z]{2})(.*)$", key_path, re.I)
    if not m or m.group(1).upper() not in _REG_ROOTS:
        return True  # 不认识的前缀不拦（保守放行）
    try:
        import winreg
    except ImportError:
        return True
    root = _REG_ROOTS[m.group(1).upper()]
    sub = m.group(2).strip("\\")
    try:
        winreg.OpenKey(root, sub)
        return True
    except OSError:
        return False


def _needs_admin(normed_path: str) -> bool:
    return any(normed_path.startswith(p) for p in _ADMIN_PREFIXES)


def _parse_filekey(value: str, base_id: str, category: str,
                   warning: str, excludes: list) -> list:
    """FileKeyN=<paths>|<patterns>|<flags> → Rule 列表。"""
    parts = value.split("|")
    if len(parts) < 2:
        return []
    paths_raw, patterns_raw = parts[0], parts[1]
    flags = parts[2].strip().upper() if len(parts) > 2 else ""
    out = []
    for path_raw in paths_raw.split(";"):
        path_raw = path_raw.strip()
        if not path_raw:
            continue
        npath = _expand_winapp2_path(path_raw)
        if not npath or npath.startswith("%"):
            continue  # 变量展开失败（如 XP 专属变量），放弃该路径
        for pat in patterns_raw.split(";"):
            pat = pat.strip().replace("\\", "/")
            if not pat:
                pat = "*"
            if "REMOVESELF" in flags:
                # 整目录删：高风险，caution；本版只统计不转换，宁可少清
                stats["skipped_removeself"] += 1
                continue
            pattern = f"{npath}/**/{pat}" if "RECURSE" in flags else f"{npath}/{pat}"
            out.append(Rule(
                id=base_id, category=category, risk="review",
                title=base_id.split("|", 1)[1] if "|" in base_id else base_id,
                pattern=pattern,
                description=(warning or "winapp2 社区规则").strip(),
                needs_admin=_needs_admin(npath),
                exclude_patterns=list(excludes),
            ))
    return out


def load_winapp2(path: str) -> list:
    """解析 winapp2.ini，返回适用于本机的 Rule 列表。"""
    for k in stats:
        stats[k] = 0
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError as ex:
        raise FileNotFoundError(f"winapp2.ini 读取失败: {ex}")

    cur_name = None
    cur = {}
    out = []

    def flush():
        nonlocal cur
        if not cur_name:
            return
        stats["sections"] += 1
        # Detect/DetectFile 检查：本机没有的应用直接丢弃（几千 → 几百）
        detect_files = cur.get("DetectFile", [])
        for df in detect_files:
            p = _expand_winapp2_path(df)
            if p and not p.startswith("%") and not os.path.exists(p):
                stats["skipped_detect"] += 1
                cur = {}
                return
        for reg in cur.get("Detect", []):
            if not _detect_ok_registry(reg):
                stats["skipped_detect"] += 1
                cur = {}
                return

        warning = (cur.get("Warning", [""])[0] or "").strip()
        default_off = cur_name.rstrip().endswith("*") or \
            cur.get("Default", [""])[0].strip().lower() == "false"
        clean_name = cur_name.rstrip().rstrip("*").strip()
        slug = _SLUG_RE.sub("-", clean_name.lower())[:40] or "wa2"
        category = "app"
        for lsr in cur.get("LangSecRef", []):
            category = _LANGSEFREF_CAT.get(lsr.strip(), "app")
        excludes = []
        for ek in sorted(cur.keys()):
            if not ek.startswith("ExcludeKey"):
                continue
            parts = cur[ek][0].split("|")
            if not parts or not parts[0].strip():
                continue
            npath = _expand_winapp2_path(parts[0])
            if npath and not npath.startswith("%"):
                excludes.append(f"{npath}/**")

        idx = 0
        keys = sorted(cur.keys())
        for key in keys:
            if not key.startswith("FileKey"):
                continue
            idx += 1
            rid = f"wa2-{slug}-{idx}"
            made = _parse_filekey(cur[key][0], rid, category,
                                  warning + ("（默认不启用项）" if default_off else ""),
                                  excludes)
            for r in made:
                r.title = clean_name[:60]
            out.extend(made)
            stats["rules"] += len(made)
        for key in keys:
            if key.startswith("RegKey"):
                stats["skipped_regkey"] += cur[key].__len__()
        cur = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            cur_name = line[1:-1]
            cur = {}
            continue
        if cur_name is None or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        cur.setdefault(key, []).append(val.strip())
    flush()

    return out


def summary() -> str:
    return (f"winapp2: {stats['sections']} 节 → {stats['rules']} 条规则 "
            f"(跳过: 无Detect {stats['skipped_detect']}, REMOVESELF "
            f"{stats['skipped_removeself']}, RegKey {stats['skipped_regkey']})")


_CACHED_RULES: list = None
_CACHED_MTIME: float = 0.0


def get_winapp2_path() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "diskcleaner", "winapp2.ini")


def get_winapp2_rules() -> list:
    """获取本地 winapp2.ini 适用规则，带 mtime 缓存。文件不存在时返回空列表。"""
    global _CACHED_RULES, _CACHED_MTIME
    p = get_winapp2_path()
    if not os.path.isfile(p):
        return []
    try:
        mt = os.path.getmtime(p)
        if _CACHED_RULES is not None and _CACHED_MTIME == mt:
            return _CACHED_RULES
        rules = load_winapp2(p)
        _CACHED_RULES = rules
        _CACHED_MTIME = mt
        return rules
    except Exception:
        return []


def download_winapp2() -> tuple:
    """从上游下载最新的 winapp2.ini，更新本地缓存。返回 (ok, msg)。"""
    import urllib.request
    dest = get_winapp2_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    url = "https://raw.githubusercontent.com/bleachbit/winapp2.ini/master/Winapp2.ini"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XiaoyuToolbox/DiskCleaner"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if len(data) < 100000:
            return False, "下载文件体积异常"
        tmp_dest = dest + ".tmp"
        with open(tmp_dest, "wb") as f:
            f.write(data)
        os.replace(tmp_dest, dest)
        global _CACHED_RULES, _CACHED_MTIME
        _CACHED_RULES = None
        rules = get_winapp2_rules()
        return True, f"规则库已更新，解析出 {len(rules)} 条本机适用规则"
    except Exception as ex:
        return False, f"下载失败: {ex}"



if __name__ == "__main__":
    # 命令行自检：python -m diskcleaner.winapp2 <path-to-winapp2.ini>
    rs = load_winapp2(sys.argv[1])
    print(summary())
    for r in rs[:10]:
        print(f"  {r.risk:7} {r.id:30} {r.pattern}")
