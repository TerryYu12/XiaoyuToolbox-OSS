# -*- coding: utf-8 -*-
"""小宇工具箱 · OpenAI 专区与账号切换器 (codex_switcher.py)

功能：
1. 状态查询：读取当前活跃凭据 auth.json，解析邮箱、Account ID、过期时间与 RefreshToken 状态。
2. 账号矩阵管理：
   - 支持独立持久化存储于本地 codex_accounts.json（无需依赖外部插件，开箱即用给朋友）
   - 兼容同步 Codex++ settings.json（若本机存在，双向同步 relayProfiles）
   - 支持自由添加账号、保存当前活跃登录态为预设、删除已有预设
3. 一键切换 (1-Click Switch)：
   - 自动回写暂存当前账号最新刷新的 Token 到预设池
   - 优雅关闭 ChatGPT.exe 与 codex.exe（避免热切 abort 异常导致崩溃）
   - 原子替换目标账号的 auth.json
   - 保持 config.toml 的完整健全配置
   - 自动重新拉起 Codex Desktop 客户端
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 基础目录与系统主目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

USER_HOME = Path.home()

# 兼容标准 Windows 路径与开发环境
DEFAULT_CODEX_DIR = USER_HOME / ".codex"
if not DEFAULT_CODEX_DIR.exists() and Path(r"F:\AI-hub\agent\codex").exists():
    CODEX_DIR = Path(r"F:\AI-hub\agent\codex")
else:
    CODEX_DIR = DEFAULT_CODEX_DIR

AUTH_FILE = CODEX_DIR / "auth.json"
CONFIG_FILE = CODEX_DIR / "config.toml"
APP_AUMID = r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"

# 工具箱自有的账号矩阵持久化文件（独立运行，分享给他人无需依赖外部环境）
CODEX_ACCOUNTS_FILE = BASE_DIR / "codex_accounts.json"
# Codex++ 外部配置文件（若本机存在则双向同步兼容，不存在则忽略）
CODEX_PLUS_SETTINGS = USER_HOME / ".codex-session-delete" / "settings.json"

# OpenAI Codex 官方 OAuth 授权与配额查询常量
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
OPENAI_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"

# 配额查询缓存与线程锁
_quota_cache: dict[str, tuple[float, dict]] = {}
_quota_lock = threading.Lock()

PROFILE_ALIAS = {
    "big": "default",
    "default": "default",
    "大号": "default",
    "谷歌大": "default",
    "1": "default",
    "small": "relay-mv5bk5v6",
    "relay-mv5bk5v6": "relay-mv5bk5v6",
    "小号": "relay-mv5bk5v6",
    "谷歌小": "relay-mv5bk5v6",
    "2": "relay-mv5bk5v6"
}


def _build_proxy_opener(timeout: int = 15) -> urllib.request.OpenerDirector:
    """构建支持系统代理/Clash 本地反代的 urllib opener。"""
    proxies = {}
    env_http = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("all_proxy")
    )
    if env_http:
        proxies = {"http": env_http, "https": env_http}
    else:
        for port in (7890, 7897, 10808, 10809):
            try:
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


def _parse_jwt_payload(token_str: str) -> dict:
    if not token_str:
        return {}
    try:
        parts = token_str.split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            clean_b64 = payload.replace("-", "+").replace("_", "/")
            return json.loads(base64.b64decode(clean_b64.encode("utf-8")).decode("utf-8", "ignore"))
    except Exception:
        pass
    return {}


def parse_auth_info(auth_dict: dict) -> dict:
    tokens = auth_dict.get("tokens", {}) if isinstance(auth_dict, dict) else {}
    id_t = tokens.get("id_token", "")
    at = tokens.get("access_token", "")
    info = _parse_jwt_payload(id_t) if id_t else {}
    at_info = _parse_jwt_payload(at) if at else {}

    email = (
        info.get("email")
        or at_info.get("https://api.openai.com/profile", {}).get("email")
        or at_info.get("email")
        or tokens.get("account_email")
        or "未知邮箱"
    )
    account_id = (
        tokens.get("account_id")
        or at_info.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        or info.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        or "未知ID"
    )
    plan_type = (
        at_info.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type")
        or info.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type")
        or "plus"
    )
    exp = at_info.get("exp") or info.get("exp")
    exp_minutes = None
    is_expired = False
    if exp:
        diff = exp - time.time()
        exp_minutes = int(diff // 60)
        is_expired = diff <= 0

    return {
        "account_id": account_id,
        "email": email,
        "plan_type": plan_type,
        "exp_timestamp": exp,
        "exp_minutes": exp_minutes,
        "is_expired": is_expired,
        "has_refresh": bool(tokens.get("refresh_token")),
        "last_refresh": auth_dict.get("last_refresh")
    }


def exchange_codex_rt(rt: str) -> dict:
    """使用 rt_ 或 rt. 开头的 OpenAI Refresh Token 向官方端点交换最新凭据。"""
    clean_rt = (rt or "").strip()
    if not clean_rt:
        return {"ok": False, "error": "刷新令牌 (RT) 不能为空"}

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": clean_rt,
        "client_id": OPENAI_OAUTH_CLIENT_ID,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENAI_TOKEN_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        }
    )

    opener = _build_proxy_opener()
    try:
        with opener.open(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", "replace")
        return {"ok": False, "error": f"OpenAI OAuth 拒绝 ({e.code}): {err_msg[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"网络请求失败: {type(e).__name__} {e}"}

    access_token = data.get("access_token")
    if not access_token:
        return {"ok": False, "error": "返回未包含有效 access_token"}

    new_refresh_token = data.get("refresh_token") or clean_rt
    id_token = data.get("id_token") or ""
    expires_in = data.get("expires_in", 3600)

    # 从 id_token 与 access_token 中解析 email 与 account_id
    id_info = _parse_jwt_payload(id_token) if id_token else {}
    acc_info = _parse_jwt_payload(access_token)

    email = (
        id_info.get("email")
        or acc_info.get("https://api.openai.com/profile", {}).get("email")
        or acc_info.get("email")
        or "codex_user@openai.com"
    )
    account_id = (
        acc_info.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        or id_info.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        or "未知ID"
    )
    plan_type = acc_info.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type") or "plus"

    auth_dict = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "id_token": id_token,
            "account_id": account_id
        },
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    return {
        "ok": True,
        "email": email,
        "account_id": account_id,
        "plan_type": plan_type,
        "expires_in": expires_in,
        "auth_dict": auth_dict,
        "auth_content": json.dumps(auth_dict, ensure_ascii=False, indent=2),
    }


def refresh_codex_profile(profile_id: str | None = None) -> dict:
    """使用对应账号的 refresh_token 换取新令牌，并原子更新 codex_accounts.json（若处于活跃态则同时更新 auth.json）。"""
    data = load_profiles_data()
    profiles = data.get("profiles", [])

    target_profile = None
    if profile_id:
        for p in profiles:
            if p.get("id") == profile_id:
                target_profile = p
                break
        if not target_profile:
            return {"ok": False, "error": f"找不到预设 ID '{profile_id}'"}
    else:
        # 刷新当前活跃账号
        curr_auth = get_current_auth()
        active_id = data.get("active_profile_id")
        for p in profiles:
            if p.get("id") == active_id:
                target_profile = p
                break
        if not target_profile and curr_auth:
            rt = curr_auth.get("tokens", {}).get("refresh_token")
            if not rt:
                return {"ok": False, "error": "当前 auth.json 无 refresh_token，无法续期"}
            ex = exchange_codex_rt(rt)
            if not ex.get("ok"):
                return ex
            AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(AUTH_FILE, "w", encoding="utf-8") as f:
                f.write(ex["auth_content"])
            return {"ok": True, "message": f"成功刷新当前账号 {ex['email']}！", "email": ex["email"]}

    if not target_profile:
        return {"ok": False, "error": "未找到要刷新的预设"}

    p_auth_str = target_profile.get("authContents", "")
    if not p_auth_str:
        return {"ok": False, "error": "该预设凭据内容为空"}

    try:
        p_auth = json.loads(p_auth_str)
    except Exception as e:
        return {"ok": False, "error": f"凭据解析失败: {e}"}

    rt = p_auth.get("tokens", {}).get("refresh_token")
    if not rt:
        return {"ok": False, "error": f"预设「{target_profile.get('name')}」没有 refresh_token，无法自动续期"}

    ex = exchange_codex_rt(rt)
    if not ex.get("ok"):
        return ex

    target_profile["authContents"] = ex["auth_content"]
    target_profile["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_profiles_data(data)

    # 如果刷新的账号就是当前客户端正激活的账号，同步回写到 ~/.codex/auth.json
    curr_auth = get_current_auth()
    if curr_auth:
        curr_acc_id = curr_auth.get("tokens", {}).get("account_id")
        if curr_acc_id == ex["account_id"] or target_profile.get("id") == data.get("active_profile_id"):
            try:
                AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(AUTH_FILE, "w", encoding="utf-8") as f:
                    f.write(ex["auth_content"])
            except Exception:
                pass

    return {
        "ok": True,
        "message": f"已成功刷新「{target_profile.get('name')}」({ex['email']}) 凭据！",
        "email": ex["email"],
        "account_id": ex["account_id"]
    }


def get_codex_quota(profile_id: str | None = None, force: bool = False) -> dict:
    """获取指定预设或当前活跃账号的 OpenAI Codex 实时额度（Wham / Usage 5h / 7d 窗口）。"""
    target_auth = None
    target_pid = profile_id
    if profile_id:
        data = load_profiles_data()
        for p in data.get("profiles", []):
            if p.get("id") == profile_id:
                try:
                    target_auth = json.loads(p.get("authContents", "{}"))
                except Exception:
                    pass
                break
    else:
        target_auth = get_current_auth()
        if not target_auth:
            data = load_profiles_data()
            active_id = data.get("active_profile_id")
            for p in data.get("profiles", []):
                if p.get("id") == active_id:
                    target_pid = active_id
                    try:
                        target_auth = json.loads(p.get("authContents", "{}"))
                    except Exception:
                        pass
                    break

    if not target_auth or not isinstance(target_auth, dict):
        return {"ok": False, "error": "未找到有效账号凭据"}

    tokens = target_auth.get("tokens", {})
    access_token = tokens.get("access_token", "")
    account_id = tokens.get("account_id") or ""
    refresh_token = tokens.get("refresh_token", "")

    cache_key = account_id or (access_token[:20] if access_token else "default")
    now = time.time()
    if not force:
        with _quota_lock:
            cached = _quota_cache.get(cache_key)
            if cached and (now - cached[0]) < 45:
                return cached[1]

    # 检查 access_token 是否过期，若过期且有 refresh_token，静默续期
    info = parse_auth_info(target_auth)
    if (info.get("is_expired") or not access_token) and refresh_token:
        ref_res = refresh_codex_profile(target_pid)
        if ref_res.get("ok"):
            if target_pid:
                d = load_profiles_data()
                for p in d.get("profiles", []):
                    if p.get("id") == target_pid:
                        target_auth = json.loads(p.get("authContents", "{}"))
                        tokens = target_auth.get("tokens", {})
                        access_token = tokens.get("access_token", "")
                        account_id = tokens.get("account_id") or ""
                        break
            else:
                target_auth = get_current_auth() or target_auth
                tokens = target_auth.get("tokens", {})
                access_token = tokens.get("access_token", "")
                account_id = tokens.get("account_id") or ""

    if not access_token:
        return {"ok": False, "error": "凭据缺少 access_token"}

    req_headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "codex-cli",
        "Accept": "application/json",
    }
    if account_id and account_id != "未知ID":
        req_headers["ChatGPT-Account-ID"] = account_id

    req = urllib.request.Request(OPENAI_USAGE_ENDPOINT, headers=req_headers)
    opener = _build_proxy_opener()

    try:
        with opener.open(req, timeout=12) as resp:
            raw_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401 and refresh_token:
            ref_res = refresh_codex_profile(target_pid)
            if ref_res.get("ok"):
                return get_codex_quota(target_pid, force=True)
        err_msg = e.read().decode("utf-8", "replace")
        return {"ok": False, "error": f"额度查询失败 ({e.code}): {err_msg[:120]}"}
    except Exception as e:
        return {"ok": False, "error": f"网络连接异常: {type(e).__name__} {e}"}

    plan_type = raw_data.get("plan_type", "plus")
    rate_limit = raw_data.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}

    res = {
        "ok": True,
        "plan_type": plan_type,
        "primary_window": {
            "used_percent": primary.get("used_percent", 0),
            "limit_window_seconds": primary.get("limit_window_seconds", 18000),
            "reset_after_seconds": primary.get("reset_after_seconds", 0),
            "reset_at": primary.get("reset_at"),
        },
        "secondary_window": {
            "used_percent": secondary.get("used_percent", 0),
            "limit_window_seconds": secondary.get("limit_window_seconds", 604800),
            "reset_after_seconds": secondary.get("reset_after_seconds", 0),
            "reset_at": secondary.get("reset_at"),
        },
        "code_review": raw_data.get("code_review_rate_limit"),
        "updated_at": time.strftime("%H:%M:%S")
    }

    with _quota_lock:
        _quota_cache[cache_key] = (now, res)

    return res


def get_current_auth() -> dict | None:
    if not AUTH_FILE.exists():
        return None
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_profiles_data() -> dict:
    """读取账号矩阵配置。
    优先读取工具箱自身 codex_accounts.json；
    若不存在，尝试从 Codex++ settings.json 导入初始账号矩阵；
    若皆无，初始化空结构。
    """
    if CODEX_ACCOUNTS_FILE.exists():
        try:
            with open(CODEX_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "profiles" in data:
                    return data
        except Exception:
            pass

    # 首次自动从 Codex++ settings.json 导入已有预设
    if CODEX_PLUS_SETTINGS.exists():
        try:
            with open(CODEX_PLUS_SETTINGS, "r", encoding="utf-8") as f:
                settings = json.load(f)
                imported = []
                for p in settings.get("relayProfiles", []):
                    auth_str = p.get("authContents", "")
                    if auth_str:
                        try:
                            p_auth = json.loads(auth_str)
                            info = parse_auth_info(p_auth)
                            # 仅导入含有有效 tokens/account_id 的真实 Codex 账号
                            if info.get("account_id") != "未知ID" or info.get("has_refresh"):
                                imported.append({
                                    "id": p.get("id"),
                                    "name": p.get("name", "未命名账号"),
                                    "authContents": auth_str,
                                    "configContents": p.get("configContents", ""),
                                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
                                })
                        except Exception:
                            pass

                init_data = {
                    "active_profile_id": settings.get("activeRelayId", "default"),
                    "profiles": imported
                }
                save_profiles_data(init_data)
                return init_data
        except Exception:
            pass

    # 若皆无，尝试从当前活跃 auth.json 初始化首个预设
    curr_auth = get_current_auth()
    profiles = []
    active_id = ""
    if curr_auth:
        info = parse_auth_info(curr_auth)
        if info.get("account_id") != "未知ID":
            name = (info.get("email") or "我的账号").split("@")[0]
            active_id = "default"
            cfg_str = ""
            if CONFIG_FILE.exists():
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        cfg_str = f.read()
                except Exception:
                    pass
            profiles.append({
                "id": active_id,
                "name": name,
                "authContents": json.dumps(curr_auth, ensure_ascii=False, indent=2),
                "configContents": cfg_str,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
            })

    init_data = {
        "active_profile_id": active_id,
        "profiles": profiles
    }
    if profiles:
        save_profiles_data(init_data)
    return init_data


def save_profiles_data(data: dict) -> bool:
    """原子保存账号矩阵至 codex_accounts.json，并同步 Codex++ settings.json（若存在）。"""
    tmp = CODEX_ACCOUNTS_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CODEX_ACCOUNTS_FILE)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    # 若本机有 Codex++，同步 relayProfiles 保障共存
    if CODEX_PLUS_SETTINGS.exists():
        try:
            with open(CODEX_PLUS_SETTINGS, "r", encoding="utf-8") as f:
                settings = json.load(f)
            
            existing = settings.get("relayProfiles", [])
            retained = []
            for p in existing:
                auth_str = p.get("authContents", "")
                if auth_str:
                    try:
                        p_auth = json.loads(auth_str)
                        info = parse_auth_info(p_auth)
                        if info.get("account_id") == "未知ID" and not info.get("has_refresh"):
                            retained.append(p)
                    except Exception:
                        retained.append(p)
                else:
                    retained.append(p)

            # 合并当前账号矩阵
            synced_list = list(data.get("profiles", []))
            final_profiles = []
            for cp in synced_list:
                final_profiles.append({
                    "id": cp.get("id"),
                    "name": cp.get("name"),
                    "upstreamBaseUrl": "",
                    "protocol": "responses",
                    "relayMode": "official",
                    "officialMixApiKey": False,
                    "noAuth": False,
                    "hideOfficialUsageAlert": True,
                    "testModel": "",
                    "configContents": cp.get("configContents", ""),
                    "authContents": cp.get("authContents", "")
                })
            final_profiles.extend(retained)

            settings["relayProfiles"] = final_profiles
            if data.get("active_profile_id"):
                settings["activeRelayId"] = data.get("active_profile_id")

            s_tmp = CODEX_PLUS_SETTINGS.with_suffix(".json.tmp")
            with open(s_tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            os.replace(s_tmp, CODEX_PLUS_SETTINGS)
        except Exception:
            pass

    return True


def get_codex_status() -> dict:
    curr_auth = get_current_auth()
    data = load_profiles_data()
    active_profile_id = data.get("active_profile_id", "")

    curr_info = parse_auth_info(curr_auth) if curr_auth else None

    # 如果 active_profile_id 没对上，但 curr_info 的 account_id 匹配到了某个 profile，自动校准
    if curr_info and curr_info.get("account_id") and curr_info.get("account_id") != "未知ID":
        curr_acc_id = curr_info["account_id"]
        for p in data.get("profiles", []):
            try:
                p_auth = json.loads(p.get("authContents", "{}"))
                if p_auth.get("tokens", {}).get("account_id") == curr_acc_id:
                    active_profile_id = p.get("id")
                    break
            except Exception:
                pass

    active_quota = None
    if curr_info and curr_info.get("account_id"):
        with _quota_lock:
            cached = _quota_cache.get(curr_info["account_id"])
            if cached:
                active_quota = cached[1]

    profiles = []
    for p in data.get("profiles", []):
        pid = p.get("id")
        pname = p.get("name")
        auth_str = p.get("authContents")
        p_info = None
        p_quota = None
        if auth_str:
            try:
                p_auth = json.loads(auth_str)
                p_info = parse_auth_info(p_auth)
                acc_id = p_info.get("account_id")
                if acc_id:
                    with _quota_lock:
                        c = _quota_cache.get(acc_id)
                        if c:
                            p_quota = c[1]
            except Exception:
                pass

        profiles.append({
            "id": pid,
            "name": pname,
            "is_active": (pid == active_profile_id),
            "info": p_info,
            "quota": p_quota,
            "has_auth": bool(auth_str),
            "updated_at": p.get("updated_at")
        })

    return {
        "ok": True,
        "active_profile_id": active_profile_id,
        "current_active": curr_info,
        "active_quota": active_quota,
        "profiles": profiles
    }


def kill_codex_clients() -> None:
    # 优先使用 Windows 原生 taskkill，速度快且不依赖 pwsh
    subprocess.run(["taskkill", "/F", "/IM", "ChatGPT.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "codex.exe"], capture_output=True)
    time.sleep(0.5)


def launch_codex_desktop() -> None:
    try:
        subprocess.Popen(["explorer.exe", APP_AUMID])
    except Exception:
        pass


def save_current_as_profile(name: str = "", profile_id: str | None = None) -> dict:
    """将当前客户端活跃的登录态直接保存为账号预设。"""
    curr_auth = get_current_auth()
    if not curr_auth:
        return {"ok": False, "error": "本地未找到 Codex auth.json，请先在 Codex Desktop 登录账号"}

    info = parse_auth_info(curr_auth)
    email = info.get("email") or "codex_user"
    acc_id = info.get("account_id") or "未知ID"

    if not name or not name.strip():
        name = email.split("@")[0] if "@" in email else email

    name = name.strip()
    data = load_profiles_data()
    profiles = data.get("profiles", [])

    auth_str = json.dumps(curr_auth, ensure_ascii=False, indent=2)
    cfg_str = ""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg_str = f.read()
        except Exception:
            pass

    target = None
    if profile_id:
        for p in profiles:
            if p.get("id") == profile_id:
                target = p
                break
    else:
        # 检查是否已存在相同 account_id 或相同 email 的预设进行更新
        for p in profiles:
            try:
                pa = json.loads(p.get("authContents", "{}"))
                p_tokens = pa.get("tokens", {})
                p_acc_id = p_tokens.get("account_id")
                p_info = parse_auth_info(pa)
                p_email = p_info.get("email")
                if (acc_id != "未知ID" and p_acc_id == acc_id) or (email != "未知邮箱" and p_email == email):
                    target = p
                    break
            except Exception:
                pass

    if target:
        target["name"] = name
        target["authContents"] = auth_str
        if cfg_str:
            target["configContents"] = cfg_str
        target["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        target_id = target["id"]
    else:
        target_id = f"acc-{int(time.time())}"
        target = {
            "id": target_id,
            "name": name,
            "authContents": auth_str,
            "configContents": cfg_str,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        profiles.append(target)

    data["active_profile_id"] = target_id
    data["profiles"] = profiles
    save_profiles_data(data)

    return {
        "ok": True,
        "id": target_id,
        "name": name,
        "email": email,
        "message": f"成功保存账号预设「{name}」({email})！"
    }


def add_or_update_profile(name: str, auth_content: str, profile_id: str | None = None) -> dict:
    """手动录入或更新账号凭据。"""
    if not name or not name.strip():
        return {"ok": False, "error": "请输入预设名称"}
    if not auth_content or not auth_content.strip():
        return {"ok": False, "error": "请输入或导入 auth.json 内容"}

    try:
        auth_dict = json.loads(auth_content.strip())
        if not isinstance(auth_dict, dict) or "tokens" not in auth_dict:
            return {"ok": False, "error": "凭据格式不正确：必须是包含 tokens 对象的 auth.json 结构"}
    except Exception as e:
        return {"ok": False, "error": f"JSON 解析失败: {e}"}

    name = name.strip()
    data = load_profiles_data()
    profiles = data.get("profiles", [])

    info = parse_auth_info(auth_dict)
    email = info.get("email") or ""

    target = None
    if profile_id:
        for p in profiles:
            if p.get("id") == profile_id:
                target = p
                break
    else:
        # 自动查重，按 email 或 account_id 匹配
        p_acc_id = auth_dict.get("tokens", {}).get("account_id")
        for p in profiles:
            try:
                pa = json.loads(p.get("authContents", "{}"))
                existing_info = parse_auth_info(pa)
                if (email and email != "未知邮箱" and existing_info.get("email") == email) or (
                    p_acc_id and p_acc_id != "未知ID" and pa.get("tokens", {}).get("account_id") == p_acc_id
                ):
                    target = p
                    break
            except Exception:
                pass

    auth_str = json.dumps(auth_dict, ensure_ascii=False, indent=2)

    if target:
        target["name"] = name
        target["authContents"] = auth_str
        target["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        target_id = target["id"]
    else:
        target_id = profile_id or f"acc-{int(time.time())}"
        profiles.append({
            "id": target_id,
            "name": name,
            "authContents": auth_str,
            "configContents": "",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        })

    data["profiles"] = profiles
    save_profiles_data(data)

    return {
        "ok": True,
        "id": target_id,
        "name": name,
        "email": email,
        "message": f"已成功保存预设「{name}」！"
    }


def delete_codex_profile(profile_id: str) -> dict:
    """删除指定的账号预设。"""
    if not profile_id:
        return {"ok": False, "error": "缺少要删除的预设 ID"}

    data = load_profiles_data()
    profiles = data.get("profiles", [])

    found = None
    for p in profiles:
        if p.get("id") == profile_id:
            found = p
            break

    if not found:
        return {"ok": False, "error": f"未找到 ID 为 '{profile_id}' 的预设"}

    data["profiles"] = [p for p in profiles if p.get("id") != profile_id]
    if data.get("active_profile_id") == profile_id:
        data["active_profile_id"] = data["profiles"][0]["id"] if data["profiles"] else ""

    save_profiles_data(data)
    return {"ok": True, "message": f"已成功删除预设「{found.get('name')}」"}


def reorder_codex_profiles(order_ids: list[str]) -> dict:
    """根据 ID 列表自定义重新排序账号矩阵。"""
    if not isinstance(order_ids, list):
        return {"ok": False, "error": "order_ids 必须为列表"}

    data = load_profiles_data()
    profiles = data.get("profiles", [])
    profile_map = {p.get("id"): p for p in profiles}

    reordered = []
    for pid in order_ids:
        if pid in profile_map:
            reordered.append(profile_map.pop(pid))
    for remaining in profile_map.values():
        reordered.append(remaining)

    data["profiles"] = reordered
    save_profiles_data(data)
    return {"ok": True, "message": "账号矩阵排序已更新"}


def move_codex_profile(profile_id: str, direction: str) -> dict:
    """调整预设顺序 (direction: 'up' | 'down' | 'top')。"""
    data = load_profiles_data()
    profiles = data.get("profiles", [])

    idx = -1
    for i, p in enumerate(profiles):
        if p.get("id") == profile_id:
            idx = i
            break

    if idx == -1:
        return {"ok": False, "error": f"未找到 ID 为 '{profile_id}' 的预设"}

    if direction == "top":
        if idx > 0:
            item = profiles.pop(idx)
            profiles.insert(0, item)
    elif direction == "up":
        if idx > 0:
            profiles[idx - 1], profiles[idx] = profiles[idx], profiles[idx - 1]
    elif direction == "down":
        if idx < len(profiles) - 1:
            profiles[idx], profiles[idx + 1] = profiles[idx + 1], profiles[idx]
    else:
        return {"ok": False, "error": f"未知移动方向 '{direction}'"}

    data["profiles"] = profiles
    save_profiles_data(data)
    return {"ok": True, "message": "排序已更新"}


def switch_codex_account(target_key: str) -> dict:
    target_id = PROFILE_ALIAS.get(target_key.lower()) or target_key
    data = load_profiles_data()
    profiles = data.get("profiles", [])

    target_profile = None
    for p in profiles:
        if p.get("id") == target_id or p.get("name") == target_key:
            target_profile = p
            break

    if not target_profile or not target_profile.get("authContents"):
        return {
            "ok": False,
            "error": f"找不到预设 '{target_key}' 或该预设缺少凭据"
        }

    # 1. 自动把当前活跃的最新 auth.json 回写更新给对应的旧预设
    curr_auth = get_current_auth()
    if curr_auth:
        curr_acc_id = curr_auth.get("tokens", {}).get("account_id")
        for p in profiles:
            p_auth_str = p.get("authContents")
            if p_auth_str:
                try:
                    p_dict = json.loads(p_auth_str)
                    if p_dict.get("tokens", {}).get("account_id") == curr_acc_id:
                        p["authContents"] = json.dumps(curr_auth, ensure_ascii=False, indent=2)
                        p["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        break
                except Exception:
                    pass

    # 2. 优雅停掉客户端
    kill_codex_clients()

    # 3. 写入目标凭据
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    target_auth_content = target_profile["authContents"]
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(target_auth_content)

    # 4. 保证 config.toml 最优并同步进预设
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                latest_cfg = f.read()
            target_profile["configContents"] = latest_cfg
        except Exception:
            pass

    data["active_profile_id"] = target_profile["id"]
    save_profiles_data(data)

    # 5. 重新拉起客户端
    launch_codex_desktop()

    target_info = parse_auth_info(json.loads(target_auth_content))
    return {
        "ok": True,
        "target_name": target_profile.get("name"),
        "target_email": target_info.get("email"),
        "message": f"成功切换至 {target_profile.get('name')} ({target_info.get('email')}) 并已冷启动客户端！"
    }
