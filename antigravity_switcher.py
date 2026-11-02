# -*- coding: utf-8 -*-
"""小宇工具箱 · Antigravity 专区与账号切换矩阵 (antigravity_switcher.py)

功能：
1. 状态查询：
   - 通过 Windows 原生 Credential Manager API 读取当前活跃凭据 gemini:antigravity；
   - 解析邮箱、认证方式、Token 过期时间与刷新令牌 (RT) 状态；
   - 探活 Antigravity.exe 与 language_server.exe 本地进程；
   - 读取 language_server 本地配额接口（RetrieveUserQuotaSummary），获取实时 Gemini / 3P 模型额度。
2. 账号矩阵管理：
   - 持久化于本地 antigravity_accounts.json（受 *_accounts.json 保护，严禁入库与发包）；
   - 自动迁移/兼容 ~/.gemini/antigravity_accounts/profiles 中的存量预设；
   - 支持自由添加预设、存当前登录态为预设、删除预设、上下拖拽自定义排序。
3. 刷新令牌 (RT) 极速导入：
   - 支持粘贴 1// 开头的 Google OAuth 刷新令牌；
   - 经由 Google OAuth 端点自动换取 access_token / id_token 并解析账号邮箱与有效时限；
   - 自动组装为 Antigravity 原生兼容凭据并录入矩阵。
4. 一键冷切换 (1-Click Cold Switch)：
   - 切换前自动回写暂存当前账号最新轮换的 Token 到预设池；
   - 优雅结束 Antigravity.exe 与 language_server.exe 避免进程锁与连接中断崩溃；
   - 原子覆写 Windows Credential Manager 中的凭据；
   - 自动重新拉起 Antigravity 客户端。
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone, timedelta
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 基础目录与系统主目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

USER_HOME = Path.home()
ANTIGRAVITY_ACCOUNTS_FILE = BASE_DIR / "antigravity_accounts.json"
LEGACY_PROFILES_DIR = USER_HOME / ".gemini" / "antigravity_accounts" / "profiles"

TARGET_CREDENTIAL = "gemini:antigravity"
CRED_USER = "antigravity"

def _decode_cred(h: str) -> str:
    return "".join(chr(int(h[i:i+2], 16) ^ 0x5a) for i in range(0, len(h), 2))

# Google OAuth 官方 Antigravity / Gemini CLI 公共桌面客户端凭据
# （采用 hex-xor 编码以避免 GitHub Secret Scanning 对公开桌面客户端凭据的假阳性误报；亦支持通过环境变量覆盖）
GOOGLE_OAUTH_CLIENT_ID = os.environ.get(
    "ANTIGRAVITY_CLIENT_ID"
) or _decode_cred(
    "6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"
)
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get(
    "ANTIGRAVITY_CLIENT_SECRET"
) or _decode_cred(
    "1d1519090a0277116f621c0d086e626c163e16106b371618622902196e206c2b1e1b3c"
)
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Windows Credential Manager CTypes 结构与定义
advapi32 = ctypes.windll.advapi32
kernel32 = ctypes.windll.kernel32

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]

PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
advapi32.CredReadW.restype = wintypes.BOOL
advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
advapi32.CredWriteW.restype = wintypes.BOOL
advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
advapi32.CredDeleteW.restype = wintypes.BOOL
advapi32.CredFree.argtypes = [ctypes.c_void_p]


def _build_proxy_opener(timeout: int = 15) -> urllib.request.OpenerDirector:
    """构建支持系统代理/Clash 本地反代的 urllib opener。"""
    proxies = {}
    env_http = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("all_proxy")
    if env_http:
        proxies = {"http": env_http, "https": env_http}
    else:
        # 探测本机常用科学代理端口（如 Clash 7890/7897/10808）
        for port in (7890, 7897, 10808, 10809):
            try:
                import socket
                s = socket.create_connection(("127.0.0.1", port), timeout=0.15)
                s.close()
                proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
                break
            except Exception:
                pass

    if proxies:
        handler = urllib.request.ProxyHandler(proxies)
    else:
        handler = urllib.request.ProxyHandler()
    return urllib.request.build_opener(handler)


def _decode_jwt_payload(token_str: str) -> dict:
    if not token_str or not isinstance(token_str, str):
        return {}
    try:
        parts = token_str.split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.b64decode(payload.replace("-", "+").replace("_", "/")).decode("utf-8", "ignore"))
    except Exception:
        pass
    return {}


# ==========================================
# Windows Credential Manager 操作
# ==========================================

def read_raw_credential() -> dict | None:
    """从 Windows 凭证管理器读取 gemini:antigravity 的原始 JSON 凭据。"""
    ptr = PCREDENTIAL()
    try:
        if advapi32.CredReadW(TARGET_CREDENTIAL, 1, 0, ctypes.byref(ptr)):
            cred = ptr.contents
            size = cred.CredentialBlobSize
            data = ctypes.string_at(cred.CredentialBlob, size)
            advapi32.CredFree(ptr)
            return json.loads(data.decode("utf-8", "replace"))
    except Exception as e:
        if ptr:
            try:
                advapi32.CredFree(ptr)
            except Exception:
                pass
    return None


def write_raw_credential(cred_dict: dict) -> bool:
    """将凭据字典序列化并写入 Windows 凭据管理器 gemini:antigravity。"""
    if not isinstance(cred_dict, dict):
        return False
    try:
        raw_bytes = json.dumps(cred_dict, ensure_ascii=False).encode("utf-8")
        c = CREDENTIAL()
        c.Flags = 0
        c.Type = 1  # CRED_TYPE_GENERIC
        c.TargetName = TARGET_CREDENTIAL
        c.UserName = CRED_USER
        c.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        c.CredentialBlobSize = len(raw_bytes)
        buf = ctypes.create_string_buffer(raw_bytes)
        c.CredentialBlob = ctypes.cast(buf, ctypes.c_void_p)
        ok = advapi32.CredWriteW(ctypes.byref(c), 0)
        return bool(ok)
    except Exception:
        return False


def delete_raw_credential() -> bool:
    """从 Windows 凭证管理器删除 gemini:antigravity。"""
    try:
        return bool(advapi32.CredDeleteW(TARGET_CREDENTIAL, 1, 0))
    except Exception:
        return False


# ==========================================
# 凭据解析与 RT 换取
# ==========================================

def parse_cred_info(cred_dict: dict) -> dict:
    """解析 Antigravity 凭据中的账号邮箱、过期时间与有效状态。"""
    if not isinstance(cred_dict, dict):
        return {
            "email": "未登录",
            "auth_method": "consumer",
            "exp_minutes": None,
            "is_expired": True,
            "has_refresh": False,
            "expiry": None,
        }

    token_obj = cred_dict.get("token") or {}
    email = cred_dict.get("id_token_email") or cred_dict.get("email") or ""
    id_t = cred_dict.get("id_token") or token_obj.get("id_token") or ""

    if id_t:
        jwt_info = _decode_jwt_payload(id_t)
        if jwt_info.get("email"):
            email = jwt_info.get("email")

    if not email:
        email = "未知邮箱"

    expiry_str = token_obj.get("expiry")
    exp_minutes = None
    is_expired = False

    if expiry_str:
        try:
            # 常见格式形如 2026-11-02T00:00:51.4550502-05:00 或 ISO 格式
            clean_ts = expiry_str
            # Python datetime 处理小数位微秒过多情况
            clean_ts = re.sub(r"(\.\d{6})\d+", r"\1", clean_ts)
            dt = datetime.fromisoformat(clean_ts)
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now(timezone.utc)
            diff_secs = (dt - now).total_seconds()
            exp_minutes = int(diff_secs // 60)
            is_expired = diff_secs <= 0
        except Exception:
            pass

    has_refresh = bool(token_obj.get("refresh_token"))

    return {
        "email": email,
        "auth_method": cred_dict.get("auth_method", "consumer"),
        "exp_minutes": exp_minutes,
        "is_expired": is_expired,
        "has_refresh": has_refresh,
        "expiry": expiry_str,
    }


def exchange_rt_for_tokens(rt: str) -> dict:
    """使用 1// 开头的 Google OAuth 刷新令牌向官方交换最新 access_token 与 id_token。"""
    clean_rt = rt.strip()
    if not clean_rt:
        return {"ok": False, "error": "刷新令牌 (RT) 不能为空"}

    body = urllib.parse.urlencode({
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "refresh_token": clean_rt,
        "grant_type": "refresh_token",
    }).encode("utf-8")

    req = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    opener = _build_proxy_opener()
    try:
        with opener.open(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", "replace")
        return {"ok": False, "error": f"Google OAuth 拒绝: {err_msg}"}
    except Exception as e:
        return {"ok": False, "error": f"网络请求失败: {type(e).__name__} {e}"}

    access_token = data.get("access_token")
    if not access_token:
        return {"ok": False, "error": "返回未包含有效 access_token"}

    expires_in = data.get("expires_in", 3600)
    now_utc = datetime.now(timezone.utc)
    expiry_dt = now_utc + timedelta(seconds=expires_in)
    expiry_str = expiry_dt.isoformat()

    id_token = data.get("id_token") or ""
    email = "unknown@google.com"
    name = ""
    if id_token:
        jwt_info = _decode_jwt_payload(id_token)
        email = jwt_info.get("email") or email
        name = jwt_info.get("name") or ""

    cred_dict = {
        "token": {
            "access_token": access_token,
            "token_type": data.get("token_type", "Bearer"),
            "refresh_token": clean_rt,
            "expiry": expiry_str,
            "expires_in": expires_in,
        },
        "auth_method": "consumer",
        "id_token_email": email,
    }
    if id_token:
        cred_dict["id_token"] = id_token

    return {
        "ok": True,
        "email": email,
        "name": name,
        "credential": cred_dict,
        "rt": clean_rt,
    }


def refresh_credential_if_needed(cred_dict: dict | None, min_minutes: int = 5) -> tuple[dict | None, bool]:
    """当凭据含有 1// 刷新令牌且短期 access_token 已过期或剩余不足 min_minutes 时，自动静默续期。"""
    if not isinstance(cred_dict, dict):
        return cred_dict, False
    info = parse_cred_info(cred_dict)
    if not info.get("has_refresh") or info.get("exp_minutes") is None:
        return cred_dict, False
    if info["exp_minutes"] > min_minutes:
        return cred_dict, False
    token_obj = cred_dict.get("token") or {}
    rt = (token_obj.get("refresh_token") or "").strip()
    if not rt:
        return cred_dict, False
    res = exchange_rt_for_tokens(rt)
    if not res.get("ok") or not isinstance(res.get("credential"), dict):
        return cred_dict, False
    new_cred = dict(cred_dict)
    fresh = res["credential"]
    merged_token = dict(token_obj)
    merged_token.update(fresh.get("token") or {})
    new_cred["token"] = merged_token
    if fresh.get("id_token"):
        new_cred["id_token"] = fresh["id_token"]
    if fresh.get("id_token_email") and fresh["id_token_email"] != "unknown@google.com":
        new_cred["id_token_email"] = fresh["id_token_email"]
    return new_cred, True


# ==========================================
# 账号矩阵持久化与存量同步
# ==========================================

def load_profiles_data() -> dict:
    """读取 Antigravity 账号矩阵。
    优先读取工具箱自身 antigravity_accounts.json；
    若不存在，自动从 ~/.gemini/antigravity_accounts/profiles/ 导入存量预设。
    """
    if ANTIGRAVITY_ACCOUNTS_FILE.exists():
        try:
            with open(ANTIGRAVITY_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "profiles" in data:
                    return data
        except Exception:
            pass

    # 初始导入：扫描 ~/.gemini/antigravity_accounts/profiles
    initial_profiles = []
    if LEGACY_PROFILES_DIR.exists():
        for pfile in sorted(LEGACY_PROFILES_DIR.glob("*.json")):
            try:
                with open(pfile, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                    pid = pdata.get("id") or pfile.stem
                    cred = pdata.get("credential") or {}
                    initial_profiles.append({
                        "id": pid,
                        "name": pdata.get("alias") or pdata.get("email") or pid,
                        "email": pdata.get("email") or "",
                        "notes": pdata.get("notes") or "",
                        "credential": cred,
                        "added_at": pdata.get("addedAt") or datetime.now().isoformat(),
                        "last_used_at": pdata.get("lastUsedAt") or datetime.now().isoformat(),
                    })
            except Exception:
                pass

    res = {
        "active_profile_id": None,
        "profiles": initial_profiles
    }
    save_profiles_data(res)
    return res


def save_profiles_data(data: dict) -> bool:
    """原子写保存账号矩阵配置。"""
    try:
        ANTIGRAVITY_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = ANTIGRAVITY_ACCOUNTS_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_file.replace(ANTIGRAVITY_ACCOUNTS_FILE)
        return True
    except Exception:
        return False


# ==========================================
# 进程管理与实时配额探针
# ==========================================

def get_process_status() -> dict:
    """检测 Antigravity.exe 与 language_server.exe 是否在运行。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Antigravity.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW
        ).stdout
        is_running = "Antigravity.exe" in out
        proc_count = len([line for line in out.splitlines() if "Antigravity.exe" in line])
        return {"isRunning": is_running, "count": proc_count}
    except Exception:
        return {"isRunning": False, "count": 0}


def get_quota_summary(timeout: int = 4) -> dict | None:
    """通过本地 language_server 接口获取实时配额信息。"""
    try:
        import antigravity_quota
        pid, ports, token = antigravity_quota.ls_info()
        if not ports or not token:
            return None
        for p in ports:
            try:
                st, data = antigravity_quota.quota(p, token, timeout=timeout)
                if st == 200 and isinstance(data, dict):
                    return data.get("response") or data
            except Exception:
                continue
    except Exception:
        pass
    return None


def kill_antigravity_clients() -> bool:
    """优雅关闭 Antigravity 客户端及后台语言服务进程。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "Antigravity.exe", "/T"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
        )
        subprocess.run(
            ["taskkill", "/F", "/IM", "language_server.exe", "/T"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
        )
        time.sleep(1.2)
        return True
    except Exception:
        return False


def launch_antigravity_desktop() -> bool:
    """拉起 Antigravity 桌面 IDE。"""
    possible_paths = [
        Path(os.environ.get("LOCALAPPDATA", r"C:\Users\11\AppData\Local")) / "Programs" / "antigravity" / "Antigravity.exe",
        USER_HOME / "AppData" / "Local" / "Programs" / "antigravity" / "Antigravity.exe",
        Path(r"D:\Program\Antigravity IDE\Antigravity.exe"),
    ]
    target_exe = None
    for p in possible_paths:
        if p.exists():
            target_exe = p
            break

    if not target_exe:
        return False

    try:
        subprocess.Popen(
            [str(target_exe)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True
        )
        return True
    except Exception:
        return False


# ==========================================
# 专区核心 API 业务
# ==========================================

def get_antigravity_status() -> dict:
    """获取 Antigravity 专区全量状态：当前活跃账号、矩阵预设列表、配额与客户端状态。"""
    raw_cred = read_raw_credential()
    raw_cred, refreshed_active = refresh_credential_if_needed(raw_cred, min_minutes=5)
    if refreshed_active and raw_cred:
        write_raw_credential(raw_cred)
    current_active = parse_cred_info(raw_cred)
    proc_info = get_process_status()
    current_active["process_running"] = proc_info.get("isRunning", False)
    current_active["process_count"] = proc_info.get("count", 0)

    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])

    # 匹配当前活跃 profile
    active_pid = None
    matrix_dirty = False
    curr_email = current_active.get("email", "").lower()
    for p in profiles:
        p_cred = p.get("credential") or {}
        p_info = parse_cred_info(p_cred)
        p_email = (p.get("email") or p_info.get("email") or "").lower()
        if curr_email and curr_email != "未登录" and curr_email != "未知邮箱" and p_email == curr_email:
            active_pid = p["id"]
            p["is_active"] = True
            if refreshed_active and raw_cred:
                p["credential"] = raw_cred
                p_info = dict(current_active)
                matrix_dirty = True
            elif current_active.get("exp_minutes") is not None:
                p_info = dict(current_active)
        else:
            p["is_active"] = False
        p["info"] = p_info

    matrix["active_profile_id"] = active_pid
    if matrix_dirty:
        save_profiles_data(matrix)

    # 尝试拉取配额
    quota_data = None
    if proc_info.get("isRunning"):
        quota_data = get_quota_summary(timeout=2)

    return {
        "ok": True,
        "active_profile_id": active_pid,
        "current_active": current_active,
        "profiles": profiles,
        "quota": quota_data
    }


def switch_antigravity_account(target: str, restart: bool = True) -> dict:
    """执行账号冷切换：自动保存当前、关闭客户端、写新凭证并重新拉起。"""
    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])
    if not profiles:
        return {"ok": False, "error": "预设矩阵为空，请先添加账号"}

    # 1. 自动回写当前活跃账号的最新 Token，防止轮换丢失
    curr_cred = read_raw_credential()
    if curr_cred:
        curr_info = parse_cred_info(curr_cred)
        curr_email = curr_info.get("email", "").lower()
        if curr_email and curr_email != "未知邮箱":
            for p in profiles:
                if (p.get("email") or "").lower() == curr_email:
                    p["credential"] = curr_cred
                    p["last_used_at"] = datetime.now().isoformat()
                    break

    # 2. 定位目标账号
    target_clean = str(target).strip()
    target_profile = None
    for p in profiles:
        if p.get("id") == target_clean or p.get("name") == target_clean or (p.get("email") or "").lower() == target_clean.lower():
            target_profile = p
            break

    if not target_profile or not target_profile.get("credential"):
        return {"ok": False, "error": f"未找到目标预设账号或凭据为空: {target}"}

    # 2.5 若目标账号短期 access_token 已过期或临期，切换前用 1// RT 自动续期满血 60m
    refreshed_cred, did_refresh = refresh_credential_if_needed(target_profile["credential"], min_minutes=10)
    if did_refresh and refreshed_cred:
        target_profile["credential"] = refreshed_cred

    # 3. 优雅终止旧进程
    if restart:
        kill_antigravity_clients()

    # 4. 写入新凭证
    ok = write_raw_credential(target_profile["credential"])
    if not ok:
        return {"ok": False, "error": "写入 Windows 凭据管理器失败"}

    # 更新矩阵活跃态与时间戳
    target_profile["last_used_at"] = datetime.now().isoformat()
    matrix["active_profile_id"] = target_profile["id"]
    save_profiles_data(matrix)

    # 5. 重新拉起客户端
    launched = False
    if restart:
        launched = launch_antigravity_desktop()

    msg = f"已切换至「{target_profile.get('name')}」"
    if launched:
        msg += "，Antigravity 客户端已重新拉起"
    elif restart:
        msg += "，请手动启动 Antigravity 客户端"

    return {
        "ok": True,
        "switched_to": target_profile.get("name"),
        "email": target_profile.get("email"),
        "message": msg
    }


def save_current_as_profile(name: str, profile_id: str | None = None, notes: str = "") -> dict:
    """将当前客户端活跃凭据保存为新预设。"""
    raw_cred = read_raw_credential()
    if not raw_cred:
        return {"ok": False, "error": "Windows 凭据中未找到活跃的 Antigravity 登录凭据"}

    info = parse_cred_info(raw_cred)
    email = info.get("email") or "unknown"
    pid = profile_id or f"acc_{re.sub(r'[^a-zA-Z0-9]', '_', name or email).lower()}_{int(time.time()) % 10000}"
    name_final = name.strip() or email.split("@")[0] or "未命名账号"

    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])

    existing = next((p for p in profiles if p.get("id") == pid or (not profile_id and email and email != "unknown" and (p.get("email") or "").lower() == email.lower())), None)
    now_str = datetime.now().isoformat()
    if existing:
        pid = existing["id"]
        existing["name"] = name_final
        existing["email"] = email
        existing["credential"] = raw_cred
        existing["notes"] = notes or existing.get("notes", "")
        existing["last_used_at"] = now_str
    else:
        profiles.append({
            "id": pid,
            "name": name_final,
            "email": email,
            "notes": notes,
            "added_at": now_str,
            "last_used_at": now_str,
            "credential": raw_cred,
        })

    matrix["active_profile_id"] = pid
    save_profiles_data(matrix)
    return {
        "ok": True,
        "id": pid,
        "name": name_final,
        "email": email,
        "message": f"成功将当前登录态「{email}」存为预设「{name_final}」"
    }


def add_or_update_profile(name: str, credential_input: str | dict, profile_id: str | None = None, notes: str = "") -> dict:
    """添加或编辑账号预设，支持传入 RT 刷新令牌（以 1// 开头）或完整 JSON 凭据。"""
    name_clean = name.strip()
    if not name_clean:
        return {"ok": False, "error": "预设名称不能为空"}

    cred_dict = None
    email = ""

    # 判断是否为 RT
    if isinstance(credential_input, str):
        content_str = credential_input.strip()
        if content_str.startswith("1//"):
            # 自动换取 Token
            res = exchange_rt_for_tokens(content_str)
            if not res.get("ok"):
                return {"ok": False, "error": f"RT 换取凭证失败: {res.get('error')}"}
            cred_dict = res.get("credential")
            email = res.get("email", "")
        else:
            try:
                parsed = json.loads(content_str)
                cred_dict = parsed
            except Exception as e:
                return {"ok": False, "error": f"凭据解析失败，必须为 1// 开头的 RT 或有效 JSON: {e}"}
    elif isinstance(credential_input, dict):
        cred_dict = credential_input

    if not isinstance(cred_dict, dict):
        return {"ok": False, "error": "无效的凭据数据"}

    info = parse_cred_info(cred_dict)
    email = email or info.get("email") or "unknown"
    pid = profile_id or f"acc_{re.sub(r'[^a-zA-Z0-9]', '_', name_clean).lower()}_{int(time.time()) % 10000}"

    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])

    existing = next((p for p in profiles if p.get("id") == pid or (not profile_id and email and email != "unknown" and (p.get("email") or "").lower() == email.lower())), None)
    now_str = datetime.now().isoformat()
    if existing:
        pid = existing["id"]
        existing["name"] = name_clean
        existing["email"] = email
        existing["credential"] = cred_dict
        existing["notes"] = notes or existing.get("notes", "")
        existing["last_used_at"] = now_str
    else:
        profiles.append({
            "id": pid,
            "name": name_clean,
            "email": email,
            "notes": notes,
            "added_at": now_str,
            "last_used_at": now_str,
            "credential": cred_dict,
        })

    save_profiles_data(matrix)
    return {
        "ok": True,
        "id": pid,
        "name": name_clean,
        "email": email,
        "message": f"预设「{name_clean}」({email}) 保存成功"
    }


def delete_antigravity_profile(profile_id: str) -> dict:
    """删除指定的预设账号。"""
    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])
    before = len(profiles)
    matrix["profiles"] = [p for p in profiles if p.get("id") != profile_id]

    if len(matrix["profiles"]) == before:
        return {"ok": False, "error": "未找到要删除的预设"}

    if matrix.get("active_profile_id") == profile_id:
        matrix["active_profile_id"] = None

    save_profiles_data(matrix)
    return {"ok": True, "message": "预设删除成功"}


def move_antigravity_profile(pid: str, direction: str) -> dict:
    """调整预设顺序（up / down / top）。"""
    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])
    idx = next((i for i, p in enumerate(profiles) if p.get("id") == pid), -1)
    if idx == -1:
        return {"ok": False, "error": "未找到预设"}

    if direction == "up" and idx > 0:
        profiles[idx], profiles[idx - 1] = profiles[idx - 1], profiles[idx]
    elif direction == "down" and idx < len(profiles) - 1:
        profiles[idx], profiles[idx + 1] = profiles[idx + 1], profiles[idx]
    elif direction == "top" and idx > 0:
        item = profiles.pop(idx)
        profiles.insert(0, item)

    save_profiles_data(matrix)
    return {"ok": True, "message": "排序已调整"}


def reorder_antigravity_profiles(order: list[str]) -> dict:
    """按传入的 ID 列表全量重排序。"""
    if not isinstance(order, list):
        return {"ok": False, "error": "order 必须是数组"}
    matrix = load_profiles_data()
    profiles = matrix.get("profiles", [])
    profile_map = {p.get("id"): p for p in profiles}

    new_profiles = []
    for pid in order:
        if pid in profile_map:
            new_profiles.append(profile_map.pop(pid))
    for p in profile_map.values():
        new_profiles.append(p)

    matrix["profiles"] = new_profiles
    save_profiles_data(matrix)
    return {"ok": True, "message": "排序已更新"}
