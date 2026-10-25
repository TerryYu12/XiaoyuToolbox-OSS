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
import subprocess
import sys
import time
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


def _parse_jwt_payload(token_str: str) -> dict:
    if not token_str:
        return {}
    try:
        parts = token_str.split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.b64decode(payload))
    except Exception:
        pass
    return {}


def parse_auth_info(auth_dict: dict) -> dict:
    tokens = auth_dict.get("tokens", {}) if isinstance(auth_dict, dict) else {}
    id_t = tokens.get("id_token", "")
    info = _parse_jwt_payload(id_t)
    email = info.get("email") or tokens.get("account_email") or "未知邮箱"
    exp = info.get("exp")
    exp_minutes = None
    is_expired = False
    if exp:
        diff = exp - time.time()
        exp_minutes = int(diff // 60)
        is_expired = diff <= 0

    return {
        "account_id": tokens.get("account_id", "未知ID"),
        "email": email,
        "exp_timestamp": exp,
        "exp_minutes": exp_minutes,
        "is_expired": is_expired,
        "has_refresh": bool(tokens.get("refresh_token")),
        "last_refresh": auth_dict.get("last_refresh")
    }


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

    profiles = []
    for p in data.get("profiles", []):
        pid = p.get("id")
        pname = p.get("name")
        auth_str = p.get("authContents")
        p_info = None
        if auth_str:
            try:
                p_auth = json.loads(auth_str)
                p_info = parse_auth_info(p_auth)
            except Exception:
                pass

        profiles.append({
            "id": pid,
            "name": pname,
            "is_active": (pid == active_profile_id),
            "info": p_info,
            "has_auth": bool(auth_str),
            "updated_at": p.get("updated_at")
        })

    return {
        "ok": True,
        "active_profile_id": active_profile_id,
        "current_active": curr_info,
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
        # 检查是否已存在相同 account_id 的预设进行更新
        for p in profiles:
            try:
                pa = json.loads(p.get("authContents", "{}"))
                if pa.get("tokens", {}).get("account_id") == acc_id and acc_id != "未知ID":
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
