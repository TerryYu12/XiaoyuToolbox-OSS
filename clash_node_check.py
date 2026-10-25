# -*- coding: utf-8 -*-
"""Clash 节点 IP 质量检测：收集 Clash Verge 缓存节点，经临时隔离 Mihomo 实例
逐个探测出口 IPPure 信息并分级。检测本身只读；落盘仅限 Merge.yaml 分组写回
（先备份+内核校验）与 clash_node_reports/ 下的报告/配置导出，不直接改激活配置。

链路与参数照搬 2026-09-24 已实测跑通 381 节点的审计脚本
（collect_nodes.py / probe_runtime.py / audit_scan.py / finalize_audit.py，
见 docs/superpowers/plans/2026-09-25-clash-node-check.md §2）。
安全边界：完整出口 IP、节点凭据不落结果缓存、不进日志、不回前端；
probe.yaml（含凭据）只在检测期间存在于运行时目录，停止即删。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent

CONFIG_FILE = BASE / "clash_node_check.json"
CACHE_FILE = BASE / "clash_node_audit_cache.json"   # 命中 .gitignore 的 *_cache.json
LOG_FILE = BASE / "clash_node_check.log"
HISTORY_DIR = BASE / "clash_node_history"           # 每轮扫描一个脱敏快照（已 gitignore）
HISTORY_KEEP = 60                                    # 快照滚动保留轮数，超出删最旧
PROBE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "XiaoyuToolbox" / "node_probe"

IPPURE_URL = "https://my.ippure.com/v1/info"
# 兜底源 2026-09-25 实测：ping0.cc 主页有阿里云验证码+Cloudflare Turnstile（富字段拿不到），
# 但官方 /geo 是无反爬的纯文本接口（IP/位置/AS/商家 四行）；ip-api.com 免 key JSON
# （45 次/分钟按查询出口算，逐节点探测天然分摊），免费档仅 HTTP。
IPAPI_URL = ("http://ip-api.com/json/?fields=status,message,country,countryCode,"
             "query,as,isp,proxy,hosting,mobile")
PING0_GEO_URL = "https://ping0.cc/geo"
GROUP = "Audit"
# 工具箱自身服务在用的端口，临时实例的随机端口必须避开（外加两个常见本地服务端口）
AVOID_PORTS = {8790, 8899, 3000, 8080}
# 节点名里的"元信息"行（剩余流量/官网等），不是真节点
META_NAME_WORDS = (
    "剩余流量", "下次重置", "套餐到期", "官网", "客服", "节点全部超时",
    "请重新获取订阅", "请更新软件", "流量重置", "立即下载", "新客户端",
)
# 不是代理的组/规则类型
NON_PROXY_TYPES = {"direct", "reject", "selector", "url-test", "fallback", "load-balance"}

DEFAULT_CONFIG = {
    "verge_dir": str(Path(os.environ.get("APPDATA") or "") / "io.github.clash-verge-rev.clash-verge-rev"),
    "core_path": "",           # 留空自动推导（进程嗅探→安装路径字典，见 detect_verge_paths）；手填优先
    "skip_subscription_words": ["新谷歌"],
    "ok_threshold": 30,      # fraudScore ≤ 此值且非广播且出口国明确 → 合格
    "bad_threshold": 60,     # fraudScore > 此值 / 广播出口 / 名实不符 → 不合格
    "dual_green_threshold": 40,
    "cf_threshold": 40,
    "throttle_seconds": 1.3,  # 节点间节流，照实测值
    "concurrency": 4,         # 批量巡检并发路数：每路一个独立隔离内核实例（1-8）
    "delay_test": True,       # 控制器 API 延迟测试（generate_204，独立于 IP 质量源）
    "delay_timeout_ms": 5000,
    "delay_url": "http://www.gstatic.com/generate_204",
    "interface_name": "",     # 隔离探测出站绑定物理网卡（留空自动探测，防 TUN 劫持二次绕道）
    "ipqs_api_key": "",       # IPQualityScore API Key（选填，免费 1000 次/月）
    "ping0_api_key": "",      # Ping0 API Key（选填）
    "ipapi_is_api_key": "",   # ipapi.is API Key（选填，免费 1000 次/日）
}

# ---------- 国家词典（照搬已验证的 collect_nodes.safe_country_hint，唯一命中才信任） ----------
_COUNTRY_ALIASES = {
    "JP": ("日本", "Japan", "🇯🇵"), "SG": ("新加坡", "Singapore", "🇸🇬"),
    "TW": ("台湾", "台灣", "Taiwan", "🇹🇼"), "HK": ("香港", "Hong Kong", "🇭🇰"),
    "US": ("美国", "美國", "United States", "USA", "🇺🇸"),
    "KR": ("韩国", "韓國", "Korea", "🇰🇷"), "MY": ("马来西亚", "馬來西亞", "Malaysia", "🇲🇾"),
    "TH": ("泰国", "泰國", "Thailand", "🇹🇭"), "ID": ("印度尼西亚", "印尼", "Indonesia", "🇮🇩"),
    "PH": ("菲律宾", "菲律賓", "Philippines", "🇵🇭"), "VN": ("越南", "Vietnam", "🇻🇳"),
    "IN": ("印度", "India", "🇮🇳"), "AU": ("澳大利亚", "澳洲", "Australia", "🇦🇺"),
    "CA": ("加拿大", "Canada", "🇨🇦"), "GB": ("英国", "英國", "United Kingdom", "UK", "🇬🇧"),
    "DE": ("德国", "德國", "Germany", "🇩🇪"), "FR": ("法国", "法國", "France", "🇫🇷"),
    "NL": ("荷兰", "荷蘭", "Netherlands", "🇳🇱"), "RU": ("俄罗斯", "俄羅斯", "Russia", "🇷🇺"),
    "AE": ("阿联酋", "阿聯酋", "迪拜", "UAE", "🇦🇪"), "BR": ("巴西", "Brazil", "🇧🇷"),
    "AR": ("阿根廷", "Argentina", "🇦🇷"), "CH": ("瑞士", "Switzerland", "🇨🇭"),
    "NO": ("挪威", "Norway", "🇳🇴"), "ES": ("西班牙", "Spain", "🇪🇸"),
    "IT": ("意大利", "意大利", "Italy", "🇮🇹"), "SE": ("瑞典", "Sweden", "🇸🇪"),
    "NZ": ("新西兰", "New Zealand", "🇳🇿"), "IE": ("爱尔兰", "Ireland", "🇮🇪"),
    "TR": ("土耳其", "Turkey", "🇹🇷"), "MX": ("墨西哥", "Mexico", "🇲🇽"),
    "CN": ("中国大陆", "中國大陸", "内地", "內地", "中國", "中国"),
    "CL": ("智利", "Chile", "🇨🇱"), "PE": ("秘鲁", "Peru", "🇵🇪"),
    "CO": ("哥伦比亚", "Colombia", "🇨🇴"), "EC": ("厄瓜多尔", "厄瓜多", "Ecuador", "🇪🇨"),
    "EG": ("埃及", "Egypt", "🇪🇬"), "ZA": ("南非", "South Africa", "🇿🇦"),
    "IL": ("以色列", "Israel", "🇮🇱"), "IQ": ("伊拉克", "Iraq", "🇮🇶"),
    "BH": ("巴林", "Bahrain", "🇧🇭"), "OM": ("阿曼", "Oman", "🇴🇲"),
    "QA": ("卡塔尔", "卡達爾", "Qatar", "🇶🇦"), "NP": ("尼泊尔", "尼泊爾", "Nepal", "🇳🇵"),
    "BD": ("孟加拉", "Bangladesh", "🇧🇩"), "PK": ("巴基斯坦", "Pakistan", "🇵🇰"),
    "RS": ("塞尔维亚", "塞爾維亞", "Serbia", "🇷🇸"), "BG": ("保加利亚", "保加利亞", "Bulgaria", "🇧🇬"),
    "GR": ("希腊", "希臘", "Greece", "🇬🇷"), "AT": ("奥地利", "奧地利", "Austria", "🇦🇹"),
    "RO": ("罗马尼亚", "羅馬尼亞", "Romania", "🇷🇴"), "BE": ("比利时", "比利時", "Belgium", "🇧🇪"),
    "CZ": ("捷克", "Czech", "🇨🇿"), "DK": ("丹麦", "丹麥", "Denmark", "🇩🇰"),
    "EE": ("爱沙尼亚", "愛沙尼亞", "Estonia", "🇪🇪"), "HU": ("匈牙利", "Hungary", "🇭🇺"),
    "IS": ("冰岛", "冰島", "Iceland", "🇮🇸"), "UA": ("乌克兰", "烏克蘭", "Ukraine", "🇺🇦"),
    "KZ": ("哈萨克斯坦", "哈薩克斯坦", "Kazakhstan", "🇰🇿"), "FI": ("芬兰", "芬蘭", "Finland", "🇫🇮"),
    "PL": ("波兰", "波蘭", "Poland", "🇵🇱"), "MM": ("缅甸", "緬甸", "Myanmar", "🇲🇲"),
    "KH": ("柬埔寨", "Cambodia", "🇰🇭"), "MO": ("澳门", "澳門", "Macao", "🇲🇴"),
}

# ---------- 小工具 ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log(message: str) -> None:
    """只记启动/停止/异常级事件，绝不打印完整 IP、UA、节点凭据。"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} {message}\n")
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _safe_ip(ip: str):
    """出口 IP 脱敏：IPv4 留前两段，IPv6 留前两组。"""
    if ":" in ip:
        return ":".join(ip.split(":")[:2]) + ":…", "IPv6"
    pieces = ip.split(".")
    if len(pieces) == 4 and all(x.isdigit() for x in pieces):
        return ".".join(pieces[:2]) + ".x.x", "IPv4"
    return None, None


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().casefold()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return None


def _free_port(taken: set | None = None) -> int:
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in AVOID_PORTS and not (taken and port in taken):
            return port
    raise RuntimeError("找不到可用端口")


# ---------- 配置 ----------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(patch: dict) -> dict:
    """部分更新合并写盘；非法值抛 ValueError。"""
    if not isinstance(patch, dict):
        raise ValueError("请求格式错误")
    cfg = load_config()
    for key in ("verge_dir", "core_path"):
        if key in patch:
            val = str(patch[key] or "").strip()
            if not val:
                raise ValueError(f"{key} 不能为空")
            cfg[key] = val
    for key in ("ok_threshold", "bad_threshold", "dual_green_threshold", "cf_threshold"):
        if key in patch:
            try:
                val = int(patch[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数")
            if not 0 <= val <= 100:
                raise ValueError(f"{key} 必须在 0-100 之间")
            cfg[key] = val
    if "delay_test" in patch:
        cfg["delay_test"] = bool(patch["delay_test"])
    if "delay_timeout_ms" in patch:
        try:
            val = int(patch["delay_timeout_ms"])
        except (TypeError, ValueError):
            raise ValueError("delay_timeout_ms 必须是整数")
        if not 1000 <= val <= 15000:
            raise ValueError("delay_timeout_ms 必须在 1000-15000 之间")
        cfg["delay_timeout_ms"] = val
    if "delay_url" in patch:
        val = str(patch["delay_url"] or "").strip()
        if not val.startswith(("http://", "https://")):
            raise ValueError("delay_url 必须是 http(s) 地址")
        cfg["delay_url"] = val
    if "throttle_seconds" in patch:
        try:
            val = float(patch["throttle_seconds"])
        except (TypeError, ValueError):
            raise ValueError("throttle_seconds 必须是数字")
        if not 0.2 <= val <= 10:
            raise ValueError("throttle_seconds 必须在 0.2-10 之间")
        cfg["throttle_seconds"] = val
    if "concurrency" in patch:
        try:
            val = int(patch["concurrency"])
        except (TypeError, ValueError):
            raise ValueError("concurrency 必须是整数")
        if not 1 <= val <= 8:
            raise ValueError("concurrency 必须在 1-8 之间")
        cfg["concurrency"] = val
    if "skip_subscription_words" in patch:
        words = patch["skip_subscription_words"]
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            raise ValueError("skip_subscription_words 必须是字符串数组")
        cfg["skip_subscription_words"] = [w.strip() for w in words if w.strip()]
    for key in ("ipqs_api_key", "ping0_api_key", "ipapi_is_api_key", "ipapi_key"):
        if key in patch:
            cfg[key] = str(patch[key] or "").strip()
    if cfg["ok_threshold"] > cfg["bad_threshold"]:
        raise ValueError("合格阈值不能大于不合格阈值")
    _atomic_write(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))
    with _INV_LOCK:
        _INV["data"] = None       # 排除词/目录变了，节点清单缓存作废
    return {"ok": True, "config": cfg}


# ---------- 节点收集（照搬 collect_nodes.py，路径全部走配置） ----------

def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _is_node(item) -> bool:
    if not isinstance(item, dict):
        return False
    name = str(item.get("name", ""))
    if not name or any(w in name for w in META_NAME_WORDS):
        return False
    if not isinstance(item.get("server"), str) or not item.get("server"):
        return False
    if not item.get("port"):
        return False
    if str(item.get("type", "")).lower() in NON_PROXY_TYPES:
        return False
    return True


def _fingerprint(proxy: dict) -> str:
    stable = {k: v for k, v in proxy.items() if k != "name"}
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _country_hint(name: str):
    hits = []
    for code, terms in _COUNTRY_ALIASES.items():
        for term in terms:
            if term in name or (term.isascii() and re.search(
                    rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", name, re.I)):
                hits.append(code)
                break
    # 名称可含中转/来源前缀，只有唯一国家提示才信任
    return hits[0] if len(set(hits)) == 1 else None


def collect_nodes() -> dict:
    """扫描 Verge 缓存订阅 + 当前活动配置 + provider 缓存，去重出唯一节点清单。

    返回的清单含完整 proxy 参数（凭据），只允许留在内存/运行时目录，绝不进结果缓存。
    """
    cfg = load_config()
    verge = Path(cfg["verge_dir"])
    if not verge.is_dir():
        raise ValueError(f"Clash Verge 数据目录不存在: {verge}")
    metadata = _load_yaml(verge / "profiles.yaml")
    current = metadata.get("current")
    active_path = verge / "profiles" / f"{current}.yaml"
    skip_words = cfg["skip_subscription_words"]

    candidates: list[tuple[str, str, dict]] = []
    subscriptions: list[dict] = []

    def add_source(label: str, proxy):
        if _is_node(proxy):
            candidates.append((label, dict(proxy)))

    for profile in metadata.get("items", []) or []:
        if not isinstance(profile, dict) or profile.get("type") != "remote":
            continue
        label = str(profile.get("name", ""))
        if any(w in label for w in skip_words):
            path = verge / "profiles" / str(profile.get("file", ""))
            raw = len((_load_yaml(path).get("proxies") or [])) if path.exists() else 0
            subscriptions.append({"name": label, "raw": raw, "usable": 0, "skipped": True})
            continue
        path = verge / "profiles" / str(profile.get("file", ""))
        if not path.exists():
            subscriptions.append({"name": label, "raw": 0, "usable": 0, "skipped": False,
                                  "missing": True})
            continue
        data = _load_yaml(path)
        before = len(candidates)
        for proxy in data.get("proxies", []) or []:
            add_source(label, proxy)
        subscriptions.append({"name": label, "raw": len(data.get("proxies", []) or []),
                              "usable": len(candidates) - before, "skipped": False})

    # 活动安全配置里可能有未进远程缓存的独立节点；跳过名单订阅的同名残留
    if active_path.exists():
        active_data = _load_yaml(active_path)
        before = len(candidates)
        for proxy in active_data.get("proxies", []) or []:
            if not any(w in str(proxy.get("name", "")) for w in skip_words):
                add_source("安全节点配置", proxy)
        subscriptions.append({"name": "安全节点配置",
                              "raw": len(active_data.get("proxies", []) or []),
                              "usable": len(candidates) - before, "skipped": False})

        # provider 缓存兜底（与远程订阅重复的会按指纹去重）
        for provider_name, provider in (active_data.get("proxy-providers") or {}).items():
            raw_path = Path(str(provider.get("path", "")))
            possible = [raw_path] if raw_path.is_absolute() else [
                verge / raw_path, active_path.parent / raw_path, verge / "providers" / raw_path.name]
            cached = next((x for x in possible if x.exists()), None)
            if cached is None:
                continue
            try:
                provider_data = _load_yaml(cached)
            except Exception:                     # noqa: BLE001
                continue
            before = len(candidates)
            for proxy in provider_data.get("proxies", []) or []:
                add_source(f"provider:{provider_name}", proxy)
            subscriptions.append({"name": f"provider:{provider_name}",
                                  "raw": len(provider_data.get("proxies", []) or []),
                                  "usable": len(candidates) - before, "skipped": False})

    nodes_by_fp: dict[str, dict] = {}
    for source, proxy in candidates:
        fp = _fingerprint(proxy)
        entry = nodes_by_fp.get(fp)
        if entry is None:
            entry = {
                "fingerprint": fp,
                "probe_name": f"probe-{len(nodes_by_fp) + 1:04d}",
                "display_name": str(proxy.get("name", "")),
                "expected_country": _country_hint(str(proxy.get("name", ""))),
                "sources": [], "node_names": [],
                "proxy": proxy,
                "_display_from_provider": source.startswith("provider:"),
            }
            nodes_by_fp[fp] = entry
        entry["sources"].append(source)
        node_name = str(proxy.get("name", ""))
        if node_name and node_name not in entry["node_names"]:
            entry["node_names"].append(node_name)
        # 展示名优先取非 provider 来源的名字（provider 名常带前缀）
        if not source.startswith("provider:") and entry["_display_from_provider"]:
            entry["display_name"] = node_name
            entry["_display_from_provider"] = False
    for entry in nodes_by_fp.values():
        del entry["_display_from_provider"]

    return {
        "current_profile": active_path.name if current else None,
        "subscriptions": subscriptions,
        "candidate_records": len(candidates),
        "unique_nodes": list(nodes_by_fp.values()),
        "dedup": len(candidates) - len(nodes_by_fp),
        "collected_at": _now_iso(),
    }


_INV = {"data": None, "ts": 0.0}
_INV_LOCK = threading.Lock()
_INV_TTL = 600.0


def get_inventory(force: bool = False) -> dict:
    """节点清单带 10 分钟内存缓存（env 路由用）；扫描/重测走 force。"""
    with _INV_LOCK:
        if not force and _INV["data"] and time.time() - _INV["ts"] < _INV_TTL:
            return _INV["data"]
    data = collect_nodes()
    with _INV_LOCK:
        _INV["data"] = data
        _INV["ts"] = time.time()
    return data


# ---------- 隔离 Mihomo 运行时（照搬 probe_runtime.py） ----------

def _controller_call(info: dict, path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['controller_port']}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {info['secret']}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=4) as resp:
        return resp.status, resp.read()


_IFACE_CACHE: dict = {"ts": 0.0, "name": None, "ip": None}
_IFACE_LOCK = threading.Lock()


def _detect_physical_interface(force: bool = False) -> tuple[str | None, str | None]:
    """探测本机可用物理网卡名与出口 IP（过滤 TUN/TAP、Loopback、Tailscale 等虚拟网卡）。
    返回 (interface_name, ip)，供隔离内核设置 interface-name 绕过 TUN 全量劫持。"""
    with _IFACE_LOCK:
        now = time.monotonic()
        if not force and now - _IFACE_CACHE["ts"] < 120.0 and _IFACE_CACHE["name"]:
            return _IFACE_CACHE["name"], _IFACE_CACHE["ip"]
    try:
        import psutil
    except ImportError:
        return None, None
    candidates: list[tuple[str, str]] = []
    try:
        for name, addrs in psutil.net_if_addrs().items():
            lname = name.lower()
            if any(bad in lname for bad in ("mihomo", "clash", "tailscale", "loopback", "pseudo", "tap", "wsl")):
                continue
            for a in addrs:
                if getattr(a, "family", None) and a.family.name == "AF_INET":
                    ip = a.address
                    if ip.startswith(("127.", "169.254.", "198.18.", "198.19.")):
                        continue
                    candidates.append((name, ip))
    except Exception:
        return None, None

    # 用直连探测校验哪个候选网卡真实连通外网
    for name, ip in candidates:
        for target in (("223.5.5.5", 443), ("119.29.29.29", 443)):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            try:
                s.bind((ip, 0))
                s.connect(target)
                s.close()
                with _IFACE_LOCK:
                    _IFACE_CACHE.update({"ts": time.monotonic(), "name": name, "ip": ip})
                return name, ip
            except Exception:
                s.close()
    return None, None


_DNS_CACHE: dict[str, str] = {}
_DNS_LOCK = threading.Lock()


def _get_clash_mixed_port() -> int:
    try:
        cfg = load_config()
        verge_yaml = Path(cfg.get("verge_dir") or "") / "clash-verge.yaml"
        if verge_yaml.is_file():
            text = verge_yaml.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"mixed-port:\s*(\d+)", text)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 7890


def _resolve_domain_doh(domain: str, clash_mixed_port: int | None = None) -> str | None:
    """使用 DoH (AliDNS -> Cloudflare -> 本地 Clash 端口兜底) 解析域名为真实公网 IP。
    绕过系统 Fake-IP (198.18.0.0/16)，避免隔离内核出站直连走 TUN 假 IP。"""
    with _DNS_LOCK:
        if domain in _DNS_CACHE:
            return _DNS_CACHE[domain]
    try:
        ipaddress.ip_address(domain)
        return domain
    except ValueError:
        pass

    resolved = None
    # 1. AliDNS DoH (223.5.5.5，中国大陆直连极速)
    try:
        req = urllib.request.Request(f"https://223.5.5.5/resolve?name={domain}&type=A",
                                     headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=3.0) as r:
            data = json.loads(r.read().decode())
            ans = [a.get("data") for a in data.get("Answer", []) if a.get("type") == 1]
            if ans and ans[0]:
                resolved = ans[0]
    except Exception:
        pass

    # 2. Cloudflare DoH (1.1.1.1)
    if not resolved:
        try:
            req = urllib.request.Request(f"https://1.1.1.1/dns-query?name={domain}&type=A",
                                         headers={"Accept": "application/dns-json", "User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=3.0) as r:
                data = json.loads(r.read().decode())
                ans = [a.get("data") for a in data.get("Answer", []) if a.get("type") == 1]
                if ans and ans[0]:
                    resolved = ans[0]
        except Exception:
            pass

    # 3. 兜底经本地 Clash 端口
    if not resolved and clash_mixed_port:
        try:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{clash_mixed_port}",
                                             "https": f"http://127.0.0.1:{clash_mixed_port}"}))
            req = urllib.request.Request(f"https://1.1.1.1/dns-query?name={domain}&type=A",
                                         headers={"Accept": "application/dns-json", "User-Agent": "curl/8"})
            with op.open(req, timeout=3.5) as r:
                data = json.loads(r.read().decode())
                ans = [a.get("data") for a in data.get("Answer", []) if a.get("type") == 1]
                if ans and ans[0]:
                    resolved = ans[0]
        except Exception:
            pass

    if resolved:
        with _DNS_LOCK:
            _DNS_CACHE[domain] = resolved
    return resolved


def _resolve_node_servers(nodes: list[dict], clash_mixed_port: int | None = None) -> dict[str, str]:
    """并发批量解析节点 server 域名，返回 {domain: ip} 映射。"""
    domains = set()
    for n in nodes:
        srv = str(n.get("proxy", {}).get("server") or "").strip()
        if srv:
            try:
                ipaddress.ip_address(srv)
            except ValueError:
                domains.add(srv)
    if not domains:
        return {}

    def _worker(d):
        return d, _resolve_domain_doh(d, clash_mixed_port)

    workers = min(16, max(len(domains), 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(pool.map(_worker, domains))
    return {k: v for k, v in results.items() if v}


def _runtime_write_config(nodes: list[dict], tag: str = "", server_ips: dict | None = None,
                          iface_name: str | None = None, taken: set | None = None) -> dict:
    """写一个隔离实例的配置。并发扫描时 tag=1..N 各实例独立：随机端口经 taken 去重、
    文件名带进程号（跨进程不撞）；物理网卡探测与节点域名 DoH 解析由调用方一次算好传入。"""
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    mixed_port, controller_port = _free_port(taken), _free_port(taken)
    while controller_port == mixed_port:
        controller_port = _free_port(taken)
    secret = secrets.token_urlsafe(24)

    # 物理网卡与域名预解析（方案 A：出站绑定物理网卡绕过 TUN 劫持，预解析防 Fake-IP）；
    # 并发扫描由 _runtime_start_multi 一次算好传入，各实例复用同一结果
    if iface_name is None:
        custom_iface = (load_config().get("interface_name") or "").strip()
        iface_name = custom_iface or _detect_physical_interface()[0]
    if server_ips is None:
        server_ips = _resolve_node_servers(nodes, clash_mixed_port=_get_clash_mixed_port())

    used, proxies = set(), []
    for node in nodes:
        proxy = dict(node["proxy"])
        proxy["name"] = node["probe_name"]
        if proxy["name"] in used:
            raise RuntimeError("节点内部重名")
        used.add(proxy["name"])

        # 域名替换为真实 IP，并对 TLS 协议保留原始 SNI/servername
        orig_server = str(proxy.get("server", "")).strip()
        resolved_ip = server_ips.get(orig_server)
        if resolved_ip:
            proxy["server"] = resolved_ip
            ptype = proxy.get("type", "")
            if ptype in ("anytls", "trojan", "hysteria2", "tuic"):
                if not proxy.get("sni"):
                    proxy["sni"] = orig_server
            elif ptype in ("vless", "vmess"):
                if proxy.get("tls") and not proxy.get("servername") and not proxy.get("sni"):
                    proxy["servername"] = orig_server
            elif ptype == "ss":
                opts = proxy.get("plugin-opts")
                if isinstance(opts, dict) and not opts.get("host"):
                    opts["host"] = orig_server

        proxies.append(proxy)
    cfg = {
        "mixed-port": mixed_port, "allow-lan": False, "bind-address": "127.0.0.1",
        "mode": "rule", "log-level": "warning", "ipv6": False,
        "external-controller": f"127.0.0.1:{controller_port}", "secret": secret,
        "dns": {"enable": False, "ipv6": False},
        "proxies": proxies,
        "proxy-groups": [{"name": GROUP, "type": "select",
                          "proxies": [n["probe_name"] for n in nodes]}],
        "rules": [f"MATCH,{GROUP}"],
    }
    if iface_name:
        cfg["interface-name"] = iface_name
    suffix = f"-{os.getpid()}-{tag}" if tag else ""
    cfg_path = PROBE_DIR / f"probe{suffix}.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"mixed_port": mixed_port, "controller_port": controller_port, "secret": secret,
            "pid": None, "node_count": len(nodes), "config_path": str(cfg_path),
            "log_path": str(PROBE_DIR / f"mihomo{suffix}.log"), "log_handle": None}


def _default_core_candidates() -> list[Path]:
    """Clash Verge Rev 常见内核位置（用户级/系统级 UI 目录 + 服务模式 cores），
    不含机器特定路径。"""
    cands: list[Path] = []
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    if la:
        cands.append(Path(la) / "Programs" / "Clash Verge" / "verge-mihomo.exe")
    cands.append(Path(pf) / "Clash Verge" / "verge-mihomo.exe")
    cands.append(Path(pf86) / "Clash Verge" / "verge-mihomo.exe")
    # 服务模式（clash-verge-service）部署的内核；目录 ACL 常态锁给服务账户，
    # _probe_file 拒读时自动跳过
    cands.append(Path(r"C:\ProgramData\clash-verge-service\cores\verge-mihomo.exe"))
    return cands


def _probe_file(p: Path) -> bool:
    """is_file 的 ACL 防御版：clash-verge-service 服务模式的 cores 目录对普通用户
    stat 即 PermissionError（Python 3.11 的 is_file 只吞「不存在」类错误，权限错误
    会重抛），必须就地捕获、按不可用处理。"""
    try:
        return p.is_file()
    except OSError:
        return False


def detect_verge_paths() -> Path | None:
    """自动推导 Clash Verge Rev 内核：活动进程嗅探（verge-mihomo.exe，覆盖任意
    自定义安装位置）→ 默认安装路径字典。找不到返回 None，由设置里的手填路径兜底。
    注：服务模式部署的内核（C:\\ProgramData\\clash-verge-service\\cores）嗅探到
    路径也可能因 ACL 不可读，此时跳过、继续走字典。"""
    try:
        import psutil
        procs = list(psutil.process_iter(["name", "exe"]))
        # 1) 正在跑的内核进程 → 即真实内核路径（可能因服务模式 ACL 不可读，跳过）
        for p in procs:
            name = (p.info.get("name") or "").lower()
            if name == "verge-mihomo.exe":
                exe = p.info.get("exe")
                if exe and _probe_file(Path(exe)):
                    return Path(exe)
        # 2) Verge UI 进程的安装目录 → 同目录内核副本（覆盖任意自定义安装位置）
        for p in procs:
            name = (p.info.get("name") or "").lower()
            if name == "clash-verge.exe":
                exe = p.info.get("exe")
                if exe:
                    sibling = Path(exe).parent / "verge-mihomo.exe"
                    if _probe_file(sibling):
                        return sibling
    except Exception:                        # noqa: BLE001  psutil 不可用时静默走字典
        pass
    for c in _default_core_candidates():
        if _probe_file(c):
            return c
    return None


def _find_core() -> Path | None:
    cfg = load_config()
    custom = str(cfg.get("core_path") or "").strip()
    candidates: list[Path] = []
    if custom:
        candidates.append(Path(custom))
    detected = detect_verge_paths()
    if detected:
        candidates.append(detected)
    candidates.extend(_default_core_candidates())
    for c in candidates:
        if _probe_file(c):
            return c
    return None


_ACTIVE_PIDS: list[int] = []


def _runtime_write_marker() -> None:
    """把本进程拉起且仍存活的隔离内核 PID 列表写入 runtime.json（崩溃残留清扫用，
    secret 不落盘）。多实例并发时列表包含全部在跑实例；清死 PID 防跨轮误记。"""
    try:
        _ACTIVE_PIDS[:] = [p for p in _ACTIVE_PIDS if p and _pid_alive(p)]
        if not _ACTIVE_PIDS:
            return
        _atomic_write(PROBE_DIR / "runtime.json", json.dumps(
            {"pids": list(_ACTIVE_PIDS), "owner_pid": os.getpid()}))
    except OSError:
        pass


def _runtime_start(nodes: list[dict], tag: str = "", server_ips: dict | None = None,
                   iface_name: str | None = None, taken: set | None = None,
                   core: Path | None = None) -> dict:
    """拉起一个隔离内核实例。失败路径只杀自己刚起的进程、关日志句柄——
    临时目录由调用方（retest / _runtime_start_multi / _scan_work）统一清理。"""
    if core is None:
        core = _find_core()
    if core is None:
        raise ValueError("找不到 Mihomo 内核（verge-mihomo.exe），请在设置里确认路径")
    info = _runtime_write_config(nodes, tag=tag, server_ips=server_ips,
                                 iface_name=iface_name, taken=taken)
    test = subprocess.run([str(core), "-t", "-d", str(PROBE_DIR), "-f", info["config_path"]],
                          capture_output=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if test.returncode != 0:
        # 不回显内核诊断：可能带节点参数
        raise ValueError("隔离内核拒绝该配置（解析失败），请检查订阅缓存后重试")
    log_handle = Path(info["log_path"]).open("wb")
    proc = subprocess.Popen([str(core), "-d", str(PROBE_DIR), "-f", info["config_path"]],
                            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    info["pid"] = proc.pid
    info["log_handle"] = log_handle
    _ACTIVE_PIDS.append(proc.pid)
    _runtime_write_marker()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_handle.close()
            raise ValueError("隔离内核启动即退出，请检查内核版本或重试")
        try:
            _controller_call(info, "/version")
            return info
        except Exception:                        # noqa: BLE001
            time.sleep(0.25)
    log_handle.close()
    _kill_pid(info.get("pid"))
    raise ValueError("隔离内核控制器未就绪（25 秒超时）")


def _runtime_start_multi(pending: list[dict], concurrency: int) -> list[dict]:
    """并发扫描：节点按 round-robin 分片，每片一个独立隔离内核实例（各有
    mixed/controller 端口与选择器状态，互不干扰）。返回 [{"info":…, "nodes":…}]。
    任一实例启动失败 → 停掉已启动的全部实例、清理临时文件后抛错。"""
    core = _find_core()
    if core is None:
        raise ValueError("找不到 Mihomo 内核（verge-mihomo.exe），请在设置里确认路径")
    custom_iface = (load_config().get("interface_name") or "").strip()
    iface_name = custom_iface or _detect_physical_interface()[0]
    server_ips = _resolve_node_servers(pending, clash_mixed_port=_get_clash_mixed_port())
    shards = [pending[i::concurrency] for i in range(concurrency)]
    shards = [shard for shard in shards if shard]
    taken: set = set()
    started: list[dict] = []
    try:
        for index, shard in enumerate(shards, start=1):
            info = _runtime_start(shard, tag=str(index), server_ips=server_ips,
                                  iface_name=iface_name, taken=taken, core=core)
            taken.update({info["mixed_port"], info["controller_port"]})
            started.append({"info": info, "nodes": shard})
        return started
    except Exception:
        for item in started:
            try:
                _runtime_stop(item["info"], cleanup=False)
            except Exception:                   # noqa: BLE001
                pass
        try:
            _runtime_cleanup_files()
        except Exception:                       # noqa: BLE001
            pass
        raise


def _pid_alive(pid) -> bool:
    """Windows 只读探活。**os.kill(pid, 0) 在 Windows 上不是探活**——CPython 把它实现成
    OpenProcess + TerminateProcess(pid, 0)，存在即杀（误杀被复用 PID 的无辜进程）。
    用 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess，只读无副作用。"""
    if not pid:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p   # 64 位句柄必须显式声明（项目规约 2.4）
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                         # noqa: BLE001  探活失败按已死处理
        return False


def _pid_image_name(pid) -> str:
    """进程映像全路径（读不到返回空串），供残留清扫做「含 mihomo 才杀」校验。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_ulong(512)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                         # noqa: BLE001
        return ""


def _kill_pid(pid) -> None:
    if not pid:
        return
    try:
        os.kill(int(pid), 15)                    # Windows 上等价 TerminateProcess（主动杀，允许）
    except (OSError, ProcessLookupError, TypeError):
        pass


def _runtime_cleanup_files() -> None:
    """删运行时目录（probe.yaml 含节点凭据，必须清）。"""
    try:
        shutil.rmtree(PROBE_DIR, ignore_errors=True)
    except OSError:
        pass


def _runtime_stop(info: dict | None, cleanup: bool = True) -> None:
    """停一个实例并等端口释放；cleanup=False 供多实例收尾（由调用方最后统一清目录）。"""
    if not info:
        return
    _kill_pid(info.get("pid"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_alive(info.get("pid")):
        time.sleep(0.2)
    handle = info.get("log_handle")
    if handle:
        try:
            handle.close()
        except OSError:
            pass
    live = []
    try:
        for port in (info.get("mixed_port"), info.get("controller_port")):
            if not port:
                continue
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", int(port))) == 0:
                    live.append(int(port))
    except OSError:
        pass
    if cleanup:
        _runtime_cleanup_files()
    if live:
        _log(f"runtime stop 警告: 端口未释放 {live}")


def _cleanup_orphan() -> None:
    """启动探测前的残留清扫：上个进程崩溃留下的 mihomo / 凭据文件。
    红线：PID 可能被 Windows 复用，必须校验映像名含 mihomo 才可终止；
    若所属扫描进程 (owner_pid) 仍然存活且不是当前进程，严禁误杀。"""
    marker = PROBE_DIR / "runtime.json"
    if not marker.exists():
        return
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        owner_pid = info.get("owner_pid")
        if owner_pid and owner_pid != os.getpid() and _pid_alive(owner_pid):
            _log(f"残留清扫跳过: 所属扫描进程 PID={owner_pid} 仍存活")
            return
        pids = info.get("pids")
        if not isinstance(pids, list):
            pids = [info.get("pid")]           # 旧版单 pid 格式兼容
        for pid in pids:
            if not _pid_alive(pid):
                continue
            image = _pid_image_name(pid).rsplit("\\", 1)[-1].lower()
            if "mihomo" in image:
                _kill_pid(pid)
                time.sleep(0.3)
            else:
                _log(f"残留 PID {pid} 已被复用为 {image or '未知进程'}，不终止，只清目录")
        _runtime_cleanup_files()
        _log("清扫了上次的残留运行时目录")
    except Exception as exc:                     # noqa: BLE001
        _log(f"残留清扫异常: {type(exc).__name__}")


# ---------- 探测源（IPPure 主源 → ip-api → Ping0 /geo 兜底；控制器延迟测试） ----------

def _select_node(info: dict, probe_name: str) -> bool:
    group = urllib.parse.quote(GROUP, safe="")
    try:
        status, _ = _controller_call(info, f"/proxies/{group}", method="PUT",
                                     payload={"name": probe_name})
        if status != 204:
            return False
        _, body = _controller_call(info, f"/proxies/{group}")
        return json.loads(body.decode("utf-8")).get("now") == probe_name
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


_CODE_BY_ZH = {term: code for code, terms in _COUNTRY_ALIASES.items() for term in terms
               if not term.isascii() and len(term) >= 2}


def _proxied_get(port: int, url: str, timeout: float = 12.0):
    """经隔离内核 mixed 端口 GET（回环目标不触系统代理）。返回 (bytes|None, err)。
    HTTPError 不重试（错误码基本确定），网络类失败隔 2s 重试 1 次——照 IPPure 实测语义。"""
    proxy = f"http://127.0.0.1:{port}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Connection": "close"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    for attempt in range(2):
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as error:
            return None, f"HTTP{error.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == 0:
                time.sleep(2)
                continue
            return None, type(error).__name__
    return None, "network_error"


def _ippure_info(port: int):
    data, error = _proxied_get(port, IPPURE_URL)
    if data is None:
        return None, error
    try:
        return json.loads(data.decode("utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid_json"


def _ipapi_info(port: int):
    """ip-api.com 免 key JSON。免费档仅 HTTP；限流 45 次/分钟按出口 IP 计，
    逐节点探测时各节点出口互不相同，实际互不影响。"""
    data, error = _proxied_get(port, IPAPI_URL, timeout=10)
    if data is None:
        return None, error
    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid_json"
    if not isinstance(payload, dict) or payload.get("status") != "success":
        message = (payload or {}).get("message") or "failed"
        return None, f"ipapi_{message}"[:60]
    return payload, None


def _ping0_info(port: int):
    """ping0.cc/geo 纯文本四行：IP / 位置 / AS 号 / 商家。主页富字段被验证码挡住，勿试。"""
    data, error = _proxied_get(port, PING0_GEO_URL, timeout=10)
    if data is None:
        return None, error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_json"
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 3:
        return None, "ping0_unexpected"
    return {"ip": lines[0], "location": lines[1],
            "asn": lines[2], "org": " ".join(lines[3:])}, None


def _country_from_location(location: str):
    """ping0 /geo 第 2 行是「国家 地区 城市」层级（实测：香港显示「中国 香港 香港」，
    日本显示「日本 东京…」）。按空白/破折号分词、词长降序前缀匹配；**先找非 CN 国家，
    全部不命中才认 CN**——否则「中国 香港」会被误判成大陆出口。「印度尼西亚」必须
    先于「印度」匹配（词长降序天然保证）。"""
    parts = re.split(r"[\s—\-·、]+", (location or "").strip())
    matched_cn = False
    for part in parts:
        for term in sorted(_CODE_BY_ZH, key=len, reverse=True):
            if part.startswith(term):
                code = _CODE_BY_ZH[term]
                if code == "CN":
                    matched_cn = True
                    break
                return code
    return "CN" if matched_cn else None


def _delay_test(info: dict, probe_name: str, cfg: dict):
    """mihomo 控制器单节点延迟测试，走该节点自身出口。返回 (ms|None, err|None)。
    cfg 关闭时返回 (None, None) = 主动跳过。"""
    if not cfg.get("delay_test", True):
        return None, None
    name = urllib.parse.quote(probe_name, safe="")
    query = urllib.parse.urlencode({"timeout": int(cfg.get("delay_timeout_ms", 5000)),
                                    "url": cfg.get("delay_url") or "http://www.gstatic.com/generate_204"})
    try:
        status, body = _controller_call(info, f"/proxies/{name}/delay?{query}")
    except urllib.error.HTTPError as error:
        return None, f"HTTP{error.code}"     # 2026-09-25 实测：节点测不通回 503、节点不存在回 404
    except (urllib.error.URLError, OSError) as error:
        return None, type(error).__name__
    if status != 200:
        return None, f"HTTP{status}"
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid_json"
    delay = data.get("delay") if isinstance(data, dict) else None
    if isinstance(delay, (int, float)) and not isinstance(delay, bool):
        return int(delay), None
    return None, "no_delay_field"


def _probe_one(info: dict, row: dict, cfg: dict) -> dict:
    """对单个节点跑延迟 + 三层 IP 源探测并更新结果行（只写脱敏字段）。
    规范字段（ip/国家/风险）由第一个成功的源填，后续源不覆盖；ippure_status
    只描述 IPPure 自己的结果，source_used 记录规范字段出处。"""
    if not _select_node(info, row["probe_name"]):
        row.update({"ippure_status": "selection_error",
                    "ippure_error": "selector_readback_mismatch"})
    else:
        time.sleep(0.3)
        delay_ms, delay_err = _delay_test(info, row["probe_name"], cfg)
        row["delay_ms"], row["delay_error"] = delay_ms, delay_err
        port = int(info["mixed_port"])
        payload, error = _ippure_info(port)
        if payload is not None:
            ip = str(payload.get("ip") or "")
            redacted, family = _safe_ip(ip)
            is_residential = _normalize_bool(payload.get("isResidential"))
            is_broadcast = _normalize_bool(payload.get("isBroadcast"))
            fraud = payload.get("fraudScore")
            if not isinstance(fraud, (int, float)) or isinstance(fraud, bool):
                fraud = None
            row.update({
                "source_used": "ippure",
                "ip_redacted": redacted, "ip_family": family,
                "country": payload.get("country"), "country_code": payload.get("countryCode"),
                "fraud_score": fraud,
                "is_residential": is_residential, "is_broadcast": is_broadcast,
                "is_native": (not is_broadcast) if isinstance(is_broadcast, bool) else None,
                "ippure_status": "ok_ipv6_no_score" if family == "IPv6" and fraud is None else "ok",
                "ippure_error": None,
            })
        else:
            row.update({"ippure_status": "network_error", "ippure_error": error})
            # 兜底 1：ip-api（有国家 + 机房/代理标志，无风险分）
            fb_payload, fb_error = _ipapi_info(port)
            if fb_payload is not None:
                redacted, family = _safe_ip(str(fb_payload.get("query") or ""))
                row.update({
                    "source_used": "ipapi",
                    "ip_redacted": redacted, "ip_family": family,
                    "country": fb_payload.get("country"),
                    "country_code": fb_payload.get("countryCode"),
                    "ipapi_status": "ok", "ipapi_error": None,
                    "ipapi_asn": fb_payload.get("as"), "ipapi_isp": fb_payload.get("isp"),
                    "ipapi_proxy": _normalize_bool(fb_payload.get("proxy")),
                    "ipapi_hosting": _normalize_bool(fb_payload.get("hosting")),
                })
            else:
                row.update({"ipapi_status": "error", "ipapi_error": fb_error})
                # 兜底 2：ping0 /geo（出口 IP + 位置 + AS/商家）
                geo, geo_error = _ping0_info(port)
                if geo is not None:
                    redacted, family = _safe_ip(str(geo.get("ip") or ""))
                    row.update({
                        "source_used": "ping0",
                        "ip_redacted": redacted, "ip_family": family,
                        "country": None, "country_code": _country_from_location(geo.get("location")),
                        "ping0_status": "ok", "ping0_error": None,
                        "ping0_asn": geo.get("asn"), "ping0_org": geo.get("org"),
                        "ping0_location": geo.get("location"),
                    })
                else:
                    row.update({"ping0_status": "error", "ping0_error": geo_error})
        if row.get("expected_country") and row.get("country_code"):
            row["label_mismatch"] = row["expected_country"] != row["country_code"]
    row["checked_at"] = _now_iso()
    return row


# ---------- 结果缓存（只存脱敏字段） ----------

_RESULTS: dict[str, dict] = {}
_RESULTS_LOCK = threading.RLock()
_CACHE_LOCK = threading.Lock()
_RESULT_FIELDS = (
    "probe_name", "display_name", "node_names", "sources", "expected_country",
    "checked_at", "ip_redacted", "ip_family", "country", "country_code",
    "fraud_score", "is_residential", "is_broadcast", "is_native",
    "ippure_status", "ippure_error", "label_mismatch",
    "cf_score", "cf_country_codes", "cf_status", "cf_error",
    "fingerprint",          # 历史快照按指纹跨轮匹配（probe 编号跨轮不可靠）
    "source_used",          # 规范字段出处：ippure / ipapi / ping0
    "delay_ms", "delay_error",
    "ipapi_status", "ipapi_error", "ipapi_asn", "ipapi_isp", "ipapi_proxy", "ipapi_hosting",
    "ping0_status", "ping0_error", "ping0_asn", "ping0_org", "ping0_location",
)


def _load_cache() -> None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            with _RESULTS_LOCK:
                _RESULTS.clear()
                for k, v in data.items():
                    if isinstance(v, dict):
                        fp = v.get("fingerprint") or k
                        _RESULTS[fp] = v
    except (OSError, json.JSONDecodeError):
        pass


def _save_cache() -> None:
    with _CACHE_LOCK:                          # 并发 worker 同时落盘会争用同一 .tmp 路径
        with _RESULTS_LOCK:
            snapshot = {k: {f: v.get(f) for f in _RESULT_FIELDS} for k, v in _RESULTS.items()}
        try:
            _atomic_write(CACHE_FILE, json.dumps(snapshot, ensure_ascii=False, indent=1))
        except OSError as exc:
            _log(f"结果缓存写盘失败: {type(exc).__name__}")


def _make_row(node: dict) -> dict:
    return {
        "probe_name": node["probe_name"], "display_name": node.get("display_name", ""),
        "node_names": node.get("node_names", []), "sources": node.get("sources", []),
        "expected_country": node.get("expected_country"), "checked_at": None,
        "ip_redacted": None, "ip_family": None, "country": None, "country_code": None,
        "fraud_score": None, "is_residential": None, "is_broadcast": None, "is_native": None,
        "ippure_status": "pending", "ippure_error": None,
        "cf_score": None, "cf_country_codes": [], "cf_status": "pending", "cf_error": None,
        "label_mismatch": None, "fingerprint": node.get("fingerprint"),
        "source_used": None, "delay_ms": None, "delay_error": None,
        "ipapi_status": "pending", "ipapi_error": None,
        "ipapi_asn": None, "ipapi_isp": None, "ipapi_proxy": None, "ipapi_hosting": None,
        "ping0_status": "pending", "ping0_error": None,
        "ping0_asn": None, "ping0_org": None, "ping0_location": None,
    }


def _classify(row: dict, cfg: dict) -> str:
    """分级口径（阈值来自配置）：IPPure 成功 → 按 fraud/广播/名实三判；
    兜底源拿到出口国但无风险分 → 观察（名实不符仍判不合格）；
    全部源失败 → 查询失败。"""
    if str(row.get("ippure_status", "")).startswith("ok"):
        score = row.get("fraud_score")
        numeric = isinstance(score, (int, float)) and not isinstance(score, bool)
        if numeric and score > cfg["bad_threshold"]:
            return "不合格"
        if row.get("is_broadcast") is True or row.get("label_mismatch") is True:
            return "不合格"
        if (numeric and score <= cfg["ok_threshold"]
                and row.get("is_broadcast") is False and row.get("country_code")):
            return "合格"
        return "观察"
    if row.get("country_code"):
        if row.get("label_mismatch") is True:
            return "不合格"
        return "观察"
    return "查询失败"


def _public_row(row: dict, cfg: dict) -> dict:
    out = {f: row.get(f) for f in _RESULT_FIELDS}
    out["classification"] = _classify(row, cfg)
    out["dual_green"] = row.get("is_residential") is True and row.get("is_native") is True
    # 四绿 = 双绿 + IPPure<40 + CF<40；CF 检查本期未实现，恒 False
    out["four_green"] = (out["dual_green"]
                         and isinstance(row.get("fraud_score"), (int, float))
                         and not isinstance(row.get("fraud_score"), bool)
                         and row["fraud_score"] < cfg["dual_green_threshold"]
                         and isinstance(row.get("cf_score"), (int, float))
                         and not isinstance(row.get("cf_score"), bool)
                         and row["cf_score"] < cfg["cf_threshold"])
    return out


def _counts() -> dict:
    cfg = load_config()
    counts = {"ippure_ok": 0, "ippure_error": 0, "cf_ok": 0, "cf_error": 0,
              "delay_ok": 0, "dual_green": 0, "four_green": 0, "mismatch": 0,
              "classification": {"合格": 0, "观察": 0, "不合格": 0, "查询失败": 0}}
    with _RESULTS_LOCK:
        rows = list(_RESULTS.values())
    for row in rows:
        pub = _public_row(row, cfg)
        counts["ippure_ok" if str(row.get("ippure_status", "")).startswith("ok")
               else "ippure_error"] += 1
        cf_status = str(row.get("cf_status", ""))
        if cf_status == "ok":
            counts["cf_ok"] += 1
        elif cf_status and cf_status != "pending" and not cf_status.startswith("not_needed"):
            counts["cf_error"] += 1
        if isinstance(row.get("delay_ms"), int):
            counts["delay_ok"] += 1
        if pub["dual_green"]:
            counts["dual_green"] += 1
        if pub["four_green"]:
            counts["four_green"] += 1
        if row.get("label_mismatch") is True:
            counts["mismatch"] += 1
        counts["classification"][pub["classification"]] += 1
    return counts


# ---------- 历史快照层（时间轴动态图的数据源；只存脱敏事实，不含凭据/完整 IP） ----------

_HISTORY_NODE_FIELDS = (
    "fingerprint", "probe_name", "display_name", "sources", "checked_at",
    "ip_redacted", "ip_family", "country", "country_code", "fraud_score",
    "is_residential", "is_broadcast", "is_native",
    "ippure_status", "ippure_error", "label_mismatch",
    "source_used", "delay_ms", "delay_error",
)


def _save_history_snapshot(trigger: str, probed_new: int) -> str | None:
    """一轮检测结束把脱敏事实快照落盘；本轮零新探测（断点续扫没推进）不重复记轮。"""
    if probed_new <= 0:
        return None
    cfg = load_config()
    nodes = []
    with _RESULTS_LOCK:
        rows = list(_RESULTS.values())
    for row in rows:
        if not row.get("checked_at"):
            continue
        rec = {f: row.get(f) for f in _HISTORY_NODE_FIELDS}
        pub = _public_row(row, cfg)
        rec["classification"] = pub["classification"]
        rec["dual_green"] = pub["dual_green"]
        nodes.append(rec)
    if not nodes:
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    name = f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    _atomic_write(HISTORY_DIR / name, json.dumps(
        {"scanned_at": _now_iso(), "trigger": trigger, "node_count": len(nodes),
         "counts": _counts(), "nodes": nodes}, ensure_ascii=False))
    try:                                   # 滚动保留最近 HISTORY_KEEP 轮
        for old in sorted(HISTORY_DIR.glob("scan-*.json"))[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    _log(f"历史快照落盘: {name} nodes={len(nodes)}")
    return name


def history_rounds(limit: int = 30) -> dict:
    """轮次序列（旧→新，图表 x 轴即时间），只回轮次汇总不含逐节点行。"""
    try:
        files = sorted(HISTORY_DIR.glob("scan-*.json"))[-max(1, min(limit, 200)):]
    except OSError:
        files = []
    rounds = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rounds.append({"file": path.name, "scanned_at": data.get("scanned_at"),
                       "trigger": data.get("trigger"), "node_count": data.get("node_count"),
                       "counts": data.get("counts") or {}})
    return {"rounds": rounds, "kept_rounds": len(rounds)}


def history_node(node_ref: str, limit: int = 60) -> dict:
    """单节点跨轮演变。跨轮匹配必须用指纹（probe-0001 是收集顺序编号，
    两轮之间订阅增减会让同一编号指向不同节点，绝不能拿它当主键）。"""
    node_ref = (node_ref or "").strip()
    if not node_ref:
        raise ValueError("node 必填（probe 编号 / 显示名 / 节点名 / 指纹）")
    fingerprint = None
    display = node_ref
    if re.fullmatch(r"[0-9a-f]{64}", node_ref):
        fingerprint = node_ref                    # 前端可直接回传结果行里的指纹
    else:
        # 先查当前结果缓存（里面存着当轮扫描时的真实指纹），再退到清单
        with _RESULTS_LOCK:
            rows = list(_RESULTS.values())
        for row in rows:
            if node_ref in (row.get("probe_name"), row.get("display_name")) \
                    or node_ref in (row.get("node_names") or []):
                fingerprint = row.get("fingerprint")
                display = row.get("display_name", node_ref)
                break
        if fingerprint is None:
            try:
                inv = get_inventory()
                for node in inv["unique_nodes"]:
                    if node_ref in (node["probe_name"], node.get("display_name")) \
                            or node_ref in node.get("node_names", []):
                        fingerprint = node["fingerprint"]
                        display = node.get("display_name", node_ref)
                        break
            except Exception:                     # noqa: BLE001  收集失败退化为按名字匹配
                pass
    series = []
    try:
        files = sorted(HISTORY_DIR.glob("scan-*.json"))[-max(1, min(limit, 200)):]
    except OSError:
        files = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for rec in data.get("nodes") or []:
            hit = (fingerprint and rec.get("fingerprint") == fingerprint) \
                or (not fingerprint and rec.get("display_name") == node_ref)
            if not hit:
                continue
            series.append({"scanned_at": data.get("scanned_at"),
                           "checked_at": rec.get("checked_at"),
                           "classification": rec.get("classification"),
                           "fraud_score": rec.get("fraud_score"),
                           "country_code": rec.get("country_code"),
                           "ip_redacted": rec.get("ip_redacted"),
                           "dual_green": rec.get("dual_green")})
            break
    return {"node": {"display_name": display, "fingerprint": fingerprint},
            "series": series}


# ---------- 扫描线程（照 ai_fetcher._rank_refresh 的状态模式） ----------

_STATE = {"running": False, "phase": "idle",
          "progress": {"done": 0, "total": 0, "current_display": None},
          "started_at": None, "finished_at": None, "error": None,
          "scope": None, "completed": False, "concurrency": None}
_STATE_LOCK = threading.Lock()
_SCAN_GATE = threading.Lock()
_STOP_EVENT = threading.Event()


def start_scan(subscriptions: list | None = None) -> bool:
    """subscriptions 非空时只扫命中这些来源标签的节点（节点可能被多个订阅包含，
    按「sources 与所选范围有交集」筛选，同一指纹节点算同一个）。"""
    if subscriptions is not None and (
            not isinstance(subscriptions, list)
            or not all(isinstance(s, str) and s.strip() for s in subscriptions)):
        raise ValueError("subscriptions 必须是非空字符串数组")
    if not _SCAN_GATE.acquire(blocking=False):
        raise ValueError("正在检测，请先停止")
    try:
        _STOP_EVENT.clear()
        scope = [s.strip() for s in subscriptions] if subscriptions else None
        with _STATE_LOCK:
            _STATE.update({"running": True, "phase": "collecting",
                           "progress": {"done": 0, "total": 0, "current_display": None},
                           "started_at": _now_iso(), "finished_at": None, "error": None,
                           "scope": scope, "completed": False, "concurrency": None})
        threading.Thread(target=_scan_work, daemon=True).start()
        return True
    except Exception:
        _SCAN_GATE.release()
        raise


def stop_scan() -> dict:
    if not _STATE.get("running"):
        return {"ok": True, "note": "当前没有进行中的检测"}
    _STOP_EVENT.set()
    return {"ok": True, "note": "已请求停止，正在收尾（清理隔离内核与临时文件）"}


def reset_results() -> dict:
    if _STATE.get("running"):
        raise ValueError("检测进行中，不能清空结果")
    with _RESULTS_LOCK:
        _RESULTS.clear()
    try:
        CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


def _set_phase(phase: str) -> None:
    with _STATE_LOCK:
        _STATE["phase"] = phase


def _set_progress(done: int, total: int, current: str | None) -> None:
    with _STATE_LOCK:
        _STATE["progress"] = {"done": done, "total": total, "current_display": current}


def _normalize_concurrency(value) -> int:
    """并发数配置归一化：非法/缺省按 1，封顶 8（每路一个隔离内核实例）。"""
    try:
        val = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(val, 8))


def _scan_work() -> None:
    infos: list[dict] = []                    # 本轮隔离实例 [{"info":…, "nodes": shard}]
    probed_new = 0
    try:
        _log("扫描启动: 开始收集节点")
        try:
            # 2026-10-14：残留清扫移到这里执行（模块导入时执行会误杀其他进程正在
            # 使用的内核：外部 import 本模块 → 模块级清扫 → 杀掉正在跑的扫描内核 → 全灭）。
            # 启动时清理的语义也正确：清"上个进程崩溃留下的"残留。
            _cleanup_orphan()
        except Exception:                       # noqa: BLE001
            pass
        inventory = get_inventory(force=True)
        nodes = inventory["unique_nodes"]
        if not nodes:
            raise ValueError("没有收集到任何节点，请检查 Clash Verge 缓存订阅")
        with _STATE_LOCK:
            scope = _STATE.get("scope")
        if scope:
            nodes = [n for n in nodes if set(n.get("sources") or []) & set(scope)]
            if not nodes:
                raise ValueError("所选订阅范围内没有可用节点")
        cfg = load_config()
        concurrency = _normalize_concurrency(cfg.get("concurrency"))

        # 预筛（断点续扫只跳过拿到有效结果的节点：主源 ok，或 ipapi/ping0 兜底成功；
        # 且校验指纹一致，避免跨轮 probe 编号错配；network_error/selection_error 视为
        # 未完成，重扫时自动重试，否则失败结果被固化、全量扫描 2 秒空转假完成）
        pending: list[dict] = []
        done0 = 0
        for node in nodes:
            probe_name = node["probe_name"]
            fp = node.get("fingerprint") or probe_name
            with _RESULTS_LOCK:
                existing = _RESULTS.get(fp)
            if existing and existing.get("fingerprint") == node.get("fingerprint") and (
                    str(existing.get("ippure_status") or "").startswith("ok")
                    or existing.get("source_used")):
                existing["probe_name"] = probe_name
                with _RESULTS_LOCK:
                    _RESULTS[fp] = existing
                done0 += 1                     # 已有有效结果，不再重测
                continue
            with _RESULTS_LOCK:
                row = existing or _make_row(node)
                row["probe_name"] = probe_name
                _RESULTS[fp] = row
            pending.append(node)
        _set_progress(done0, len(nodes), None)

        if not pending:
            _set_phase("scanning")
            with _STATE_LOCK:
                _STATE["completed"] = True
                _STATE["concurrency"] = concurrency
            _log(f"扫描完成: 全部命中断点缓存 {done0}/{len(nodes)}（无新探测）")
        else:
            actual = min(concurrency, len(pending))
            with _STATE_LOCK:
                _STATE["concurrency"] = actual
            _set_phase("starting")
            _log(f"扫描启动: 唯一节点 {len(nodes)} 个，待测 {len(pending)} 个，"
                 f"并发 {actual} 路，正在拉起隔离内核")
            infos = _runtime_start_multi(pending, concurrency)
            _log("隔离内核就绪: " + ", ".join(
                f"#{i}(pid={it['info']['pid']} mixed={it['info']['mixed_port']} "
                f"节点数={len(it['nodes'])})" for i, it in enumerate(infos, start=1)))
            _set_phase("scanning")

            counters = {"done": done0, "probed": 0}
            outcomes: list[bool] = []

            def _worker(item: dict) -> bool:
                """一个实例顺序消化自己的分片；各实例并行。返回是否自然跑完本片。"""
                inst_info, batch = item["info"], item["nodes"]
                exhausted = True
                for node in batch:
                    if _STOP_EVENT.is_set():
                        exhausted = False
                        break
                    fp = node.get("fingerprint") or node["probe_name"]
                    with _RESULTS_LOCK:
                        row = _RESULTS.get(fp) or _make_row(node)
                        row["probe_name"] = node["probe_name"]
                        _RESULTS[fp] = row
                    with _STATE_LOCK:
                        _STATE["progress"]["current_display"] = node.get("display_name")
                    _probe_one(inst_info, row, cfg)
                    _classify(row, cfg)       # 先算一次便于落盘排查（读取时会按最新阈值重算）
                    with _RESULTS_LOCK:
                        _RESULTS[fp] = row
                    _save_cache()
                    with _STATE_LOCK:
                        counters["done"] += 1
                        counters["probed"] += 1
                        _STATE["progress"]["done"] = counters["done"]
                    _STOP_EVENT.wait(cfg["throttle_seconds"])
                return exhausted

            def _run_worker(item: dict) -> None:
                outcomes.append(_worker(item))

            threads = [threading.Thread(target=_run_worker, args=(item,), daemon=True)
                       for item in infos]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            probed_new = counters["probed"]
            if outcomes and all(outcomes):
                with _STATE_LOCK:
                    _STATE["completed"] = True
                _log(f"扫描完成: {counters['done']}/{len(nodes)}（新测 {probed_new}）")
            else:
                _log(f"扫描被用户停止: 已完成 {counters['done']}/{len(nodes)}")
    except ValueError as exc:
        with _STATE_LOCK:
            _STATE["error"] = str(exc)
        _log(f"扫描失败: {exc}")
    except Exception as exc:                   # noqa: BLE001
        with _STATE_LOCK:
            _STATE["error"] = f"{type(exc).__name__}: {exc}"[:200]
        _log(f"扫描异常: {type(exc).__name__}")
    finally:
        _set_phase("stopping")
        for item in infos:
            try:
                _runtime_stop(item["info"], cleanup=False)
            except Exception as exc:           # noqa: BLE001  2026-09-25 exe 实测：收尾线程
                _log(f"运行时清理异常: {type(exc).__name__}")   # 若在此死亡会冻结状态+永占门，必须兜底
        try:
            _runtime_cleanup_files()
        except Exception:                       # noqa: BLE001
            pass
        with _STATE_LOCK:
            completed = _STATE["completed"]
            _STATE["running"] = False
            _STATE["phase"] = "idle"
            _STATE["finished_at"] = _now_iso()
            _STATE["progress"]["current_display"] = None
        try:
            _save_history_snapshot("scan" if completed else "scan_stopped", probed_new)
        except Exception as exc:               # noqa: BLE001  快照失败不影响主流程
            _log(f"历史快照落盘异常: {type(exc).__name__}")
        if completed:
            try:                               # 检测跑完自动落一份 Markdown 报告
                _name, _text = build_report()
            except Exception as exc:           # noqa: BLE001  报告失败不影响检测结果
                _log(f"自动报告生成失败: {type(exc).__name__}")
        _SCAN_GATE.release()
        _log(f"扫描收尾: counts={_counts()}")


def retest(node_ref: str) -> dict:
    """单节点重测：只带这一个节点起临时内核，同步执行（约 10-20 秒）。"""
    if _STATE.get("running"):
        raise ValueError("检测进行中，不能单节点重测")
    if not _SCAN_GATE.acquire(blocking=False):
        raise ValueError("另一个检测/重测任务正在进行")
    info = None
    try:
        inventory = get_inventory()
        target = None
        for node in inventory["unique_nodes"]:
            if node_ref in (node["probe_name"], node.get("display_name"), node.get("fingerprint")) \
                    or node_ref in node.get("node_names", []):
                target = node
                break
        if target is None:
            raise ValueError(f"找不到节点: {node_ref}")
        try:
            # 2026-10-14：同 _scan_work——残留清扫只在"起内核前"执行
            # （模块导入时执行会误杀其他进程正在用的内核）。
            _cleanup_orphan()
        except Exception:                       # noqa: BLE001
            pass
        info = _runtime_start([target])
        fp = target.get("fingerprint") or target["probe_name"]
        with _RESULTS_LOCK:
            row = _RESULTS.get(fp) or _make_row(target)
            row["probe_name"] = target["probe_name"]
            for key in ("ippure_status", "ipapi_status", "ping0_status"):
                row[key] = "pending"
            for key in ("ippure_error", "ipapi_error", "ping0_error",
                        "label_mismatch", "source_used", "delay_ms", "delay_error"):
                row[key] = None
            _RESULTS[fp] = row
        _probe_one(info, row, load_config())
        _save_cache()
        return _public_row(row, load_config())
    finally:
        try:
            _runtime_stop(info)
        except Exception as exc:               # noqa: BLE001  同 _scan_work：兜底防冻结
            _log(f"重测清理异常: {type(exc).__name__}")
        try:                                   # _runtime_start 失败时 info 为空，也要清掉半成品目录
            _runtime_cleanup_files()
        except Exception:                       # noqa: BLE001
            pass
        _SCAN_GATE.release()


# ---------- 对外查询 ----------

def status() -> dict:
    with _STATE_LOCK:
        out = dict(_STATE)
        out["progress"] = dict(_STATE["progress"])
    out["counts"] = _counts()
    return out


_FILTER_MAP = {
    "all": lambda pub, row: True,
    "ok": lambda pub, row: pub["classification"] == "合格",
    "watch": lambda pub, row: pub["classification"] == "观察",
    "bad": lambda pub, row: pub["classification"] == "不合格",
    "fail": lambda pub, row: pub["classification"] == "查询失败",
    "dual": lambda pub, row: pub["dual_green"],
    "quad": lambda pub, row: pub["four_green"],
    "mismatch": lambda pub, row: row.get("label_mismatch") is True,
}


def results(filter_key: str = "all", query: str = "") -> dict:
    cfg = load_config()
    pred = _FILTER_MAP.get(filter_key, _FILTER_MAP["all"])
    query = (query or "").strip().casefold()
    items = []
    with _RESULTS_LOCK:
        rows = list(_RESULTS.values())
    for row in rows:
        pub = _public_row(row, cfg)
        if not pred(pub, row):
            continue
        if query:
            haystack = " ".join([str(pub.get("display_name") or ""),
                                 " ".join(pub.get("sources") or []),
                                 str(pub.get("country") or ""), str(pub.get("country_code") or "")])
            if query not in haystack.casefold():
                continue
        items.append(pub)
    return {"items": items, "total": len(items), "counts": _counts()}


def export_csv(filter_key: str = "all", query: str = "") -> tuple[str, bytes]:
    """脱敏 CSV（UTF-8 带 BOM，Excel 直开不乱码）；前 13 列照 2026-09-24 审计导出，
    末尾追加 delay_ms / source_used（兜底源可见性）。"""
    import csv
    import io
    data = results(filter_key, query)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["node", "sources", "expected_country", "exit_country",
                     "exit_country_code", "exit_ip_redacted", "ippure_score",
                     "residential", "native", "cloudflare_risk", "cloudflare_status",
                     "ippure_status", "classification", "delay_ms", "source_used"])

    def yn(value):
        return "是" if value is True else ("否" if value is False else "未知")

    for pub in data["items"]:
        writer.writerow([
            pub.get("display_name") or "", ";".join(pub.get("sources") or []),
            pub.get("expected_country") or "", pub.get("country") or "",
            pub.get("country_code") or "", pub.get("ip_redacted") or "",
            "" if pub.get("fraud_score") is None else pub.get("fraud_score"),
            yn(pub.get("is_residential")), yn(pub.get("is_native")),
            "" if pub.get("cf_score") is None else pub.get("cf_score"),
            pub.get("cf_status") or "", pub.get("ippure_status") or "",
            pub.get("classification") or "",
            "" if pub.get("delay_ms") is None else pub.get("delay_ms"),
            pub.get("source_used") or "",
        ])
    name = f"clash-node-audit-{datetime.now().strftime('%Y%m%d')}.csv"
    return name, ("\ufeff" + buf.getvalue()).encode("utf-8")


def env_status() -> dict:
    cfg = load_config()
    verge = Path(cfg["verge_dir"])
    core = _find_core()
    core_version = None
    if core is not None:
        try:
            test = subprocess.run([str(core), "-v"], capture_output=True, timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            match = re.search(rb"v[\d.]+", test.stdout or b"")
            core_version = match.group(0).decode("ascii", "ignore") if match else None
        except (OSError, subprocess.SubprocessError):
            core_version = None
    collections = {"subscriptions": [], "unique_nodes": 0, "candidate_records": 0,
                   "dedup": 0, "collected_at": None, "error": None}
    current_profile = None
    if verge.is_dir():
        try:
            inv = get_inventory()
            current_profile = inv.get("current_profile")
            collections.update({
                "subscriptions": inv["subscriptions"],
                "unique_nodes": len(inv["unique_nodes"]),
                "candidate_records": inv["candidate_records"],
                "dedup": inv["dedup"], "collected_at": inv["collected_at"],
            })
        except Exception as exc:               # noqa: BLE001
            collections["error"] = str(exc)[:200]
    with _STATE_LOCK:
        last_run = None
        if _STATE.get("started_at"):
            last_run = {"started_at": _STATE["started_at"],
                        "finished_at": _STATE["finished_at"],
                        "counts": _counts() if _STATE.get("finished_at") else None}
    return {
        "config": cfg,
        "env": {
            "verge_dir_exists": verge.is_dir(), "core_found": core is not None,
            "core_path_effective": str(core) if core else cfg["core_path"],
            "core_version": core_version, "current_profile": current_profile,
            "collections": collections, "last_run": last_run,
            "cache_updated_at": (datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
                                 .astimezone().isoformat(timespec="seconds"))
            if CACHE_FILE.exists() else None,
        },
    }


# ---------- 配置健康体检（订阅/配置结构检查 + 在线到期；只读，不改任何文件） ----------

def _subscription_userinfo(url: str) -> dict | None:
    """GET 订阅地址只取 subscription-userinfo 响应头即断开。URL 带 token，
    只在内存过手：不落盘、不进日志、不回前端（前端只见域名）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "clash-verge/1.7.2",
                                               "Connection": "close"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=12) as resp:
            raw = resp.headers.get("subscription-userinfo")
    except Exception:                        # noqa: BLE001  到期探测失败不影响体检其余项
        return None
    if not raw:
        return None
    info = {}
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key and value.strip().isdigit():
            info[key.strip()] = int(value.strip())
    return info or None


def health_check(include_online: bool = False) -> dict:
    cfg = load_config()
    verge = Path(cfg["verge_dir"])
    items = []
    if not verge.is_dir():
        return {"items": [], "error": f"Clash Verge 数据目录不存在: {verge}",
                "checked_at": _now_iso()}
    try:
        metadata = _load_yaml(verge / "profiles.yaml")
    except Exception as exc:                 # noqa: BLE001
        return {"items": [], "error": f"profiles.yaml 解析失败: {type(exc).__name__}",
                "checked_at": _now_iso()}
    for profile in metadata.get("items") or []:
        if not isinstance(profile, dict) or profile.get("type") not in ("remote", "local"):
            continue
        name = str(profile.get("name") or profile.get("file") or "(未命名)")
        path = verge / "profiles" / str(profile.get("file", ""))
        entry = {"name": name, "kind": profile.get("type"),
                 "file_exists": path.exists()}
        if path.exists():
            try:
                data = _load_yaml(path)
                proxies = [p for p in (data.get("proxies") or []) if isinstance(p, dict)]
                fps = [_fingerprint(p) for p in proxies]
                entry.update(
                    yaml_ok=True,
                    proxy_count=len(proxies),
                    unique_count=len(set(fps)),
                    duplicate_count=len(fps) - len(set(fps)),
                    group_count=len(data.get("proxy-groups") or []),
                    rule_count=len(data.get("rules") or []),
                    provider_count=len(data.get("proxy-providers") or {}),
                )
            except Exception as exc:         # noqa: BLE001
                entry.update(yaml_ok=False, parse_error=type(exc).__name__)
        url = str(profile.get("url") or "")
        if url:
            entry["host"] = urllib.parse.urlsplit(url).netloc or None
        if include_online and url:
            info = _subscription_userinfo(url)
            if info:
                used = (info.get("upload") or 0) + (info.get("download") or 0)
                total = info.get("total") or 0
                expire = info.get("expire")
                entry["traffic_used_gb"] = round(used / 2**30, 2) if used else None
                entry["traffic_total_gb"] = round(total / 2**30, 2) if total else None
                entry["expire_at"] = (datetime.fromtimestamp(expire).astimezone()
                                      .isoformat(timespec="seconds")) if expire else None
        items.append(entry)
    return {"items": items, "include_online": include_online, "checked_at": _now_iso()}


# ---------- 分级写回（全局扩展配置 Merge.yaml；红线：先备份、必确认、写前内核校验） ----------
#
# 2026-09-25 本机实况：Verge Rev 的全局扩展配置是 profiles/Merge.yaml（profiles.yaml 里
# uid=Merge, type=merge），对所有订阅生效、订阅更新不会冲掉它。写回产物 = 在它的
# append-proxy-groups 里管理三个 select 组（合格/观察/不合格节点，末位带 DIRECT），
# 组员只引用激活配置里真实存在的节点名。原文件有注释，走文本块补丁（标注起止标记），
# 绝不整体 yaml 重写（会洗掉人工注释）。

WB_GROUP_NAMES = ("合格节点", "观察节点", "不合格节点")
WB_BEGIN = "# >>> 节点检测分级写回（小宇工具箱管理段；移除请用工具箱的「移除写回」按钮）"
WB_END = "# <<< 节点检测分级写回结束"
WB_MERGE_NAME = "Merge.yaml"


def _wb_active_node_names(verge: Path, current: str) -> set:
    """激活配置最终可见的节点名 = proxies 名 + proxy-providers 缓存名（路径解析同收集）。"""
    names = set()
    active = verge / "profiles" / f"{current}.yaml"
    if not active.exists():
        return names
    data = _load_yaml(active)
    for proxy in data.get("proxies") or []:
        if isinstance(proxy, dict) and proxy.get("name"):
            names.add(str(proxy["name"]))
    for provider in (data.get("proxy-providers") or {}).values():
        raw = Path(str(provider.get("path", "")))
        possible = [raw] if raw.is_absolute() else [
            verge / raw, active.parent / raw, verge / "providers" / raw.name]
        cached = next((x for x in possible if x.exists()), None)
        if cached is None:
            continue
        try:
            pdata = _load_yaml(cached)
        except Exception:                     # noqa: BLE001
            continue
        for proxy in pdata.get("proxies") or []:
            if isinstance(proxy, dict) and proxy.get("name"):
                names.add(str(proxy["name"]))
    return names


def _wb_build_groups(verge: Path, current: str) -> tuple[dict, int]:
    """按最近检测结果产出三个分组；结果里 checked 的节点才算。"""
    cfg = load_config()
    names = _wb_active_node_names(verge, current)
    groups = {g: [] for g in WB_GROUP_NAMES}
    with _RESULTS_LOCK:
        rows = list(_RESULTS.values())
    checked = 0
    for row in rows:
        if not row.get("checked_at"):
            continue
        checked += 1
        name = None
        for candidate in [row.get("display_name")] + (row.get("node_names") or []):
            if candidate and candidate in names:
                name = candidate
                break
        if not name:
            continue
        cls = _classify(row, cfg)
        target_group = None
        if cls == "合格":
            target_group = "合格节点"
        elif cls == "观察":
            target_group = "观察节点"
        elif cls == "不合格":
            target_group = "不合格节点"
        if target_group and name not in groups[target_group]:
            groups[target_group].append(name)
    return groups, checked


def _wb_merge_path(cfg: dict) -> Path:
    return Path(cfg["verge_dir"]) / "profiles" / WB_MERGE_NAME


def _wb_read_merge_text(cfg: dict) -> str:
    path = _wb_merge_path(cfg)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _wb_split_managed(existing: dict) -> tuple[list, list]:
    """把 Merge 现有 append-proxy-groups 拆成（我们管理的、别人的）。"""
    ours, others = [], []
    for group in existing.get("append-proxy-groups") or []:
        if isinstance(group, dict) and group.get("name") in WB_GROUP_NAMES:
            ours.append(group)
        else:
            others.append(group)
    return ours, others


def _wb_groups_block(groups: dict) -> str:
    lines = [WB_BEGIN, "append-proxy-groups:"]
    for name, members in groups.items():
        lines += [f"  - name: {name}", "    type: select", "    proxies:"]
        for member in members:
            lines.append(f"      - {member}")
        lines.append("      - DIRECT")
    lines.append(WB_END)
    return "\n".join(lines)


def _wb_compose_text(original_text: str, groups: dict) -> str:
    """文本块补丁：有旧管理段则整段替换，没有则文末追加。绝不 yaml 整体重写。"""
    pattern = re.compile(re.escape(WB_BEGIN) + r".*?" + re.escape(WB_END), re.S)
    block = _wb_groups_block(groups)
    if pattern.search(original_text):
        return pattern.sub(lambda _: block, original_text)
    return original_text.rstrip("\n") + "\n\n" + block + "\n"


def _wb_combined_config(verge: Path, current: str, merge_content: dict) -> dict:
    """激活 profile 副本 + Merge 内容（近似 Verge 合并语义）→ 供写前 mihomo -t 校验。
    provider 相对路径改绝对（副本在临时目录，按 -d 解析会失联）。只留在内存/临时目录。"""
    active = verge / "profiles" / f"{current}.yaml"
    data = _load_yaml(active)
    for provider in (data.get("proxy-providers") or {}).values():
        raw = Path(str(provider.get("path", "")))
        if raw.is_absolute():
            continue
        possible = [verge / raw, active.parent / raw, verge / "providers" / raw.name]
        cached = next((x for x in possible if x.exists()), None)
        if cached is not None:
            provider["path"] = str(cached)
    groups = list(data.get("proxy-groups") or [])
    for key, mode in (("prepend-proxy-groups", "pre"), ("append-proxy-groups", "post"),
                      ("prepend-proxies", "pre"), ("append-proxies", "post"),
                      ("prepend-rules", "pre"), ("append-rules", "post")):
        extra = merge_content.get(key)
        if not extra:
            continue
        base_key = key.replace("prepend-", "").replace("append-", "")
        if mode == "pre":
            data[base_key] = list(extra) + list(data.get(base_key) or [])
        else:
            data[base_key] = list(data.get(base_key) or []) + list(extra)
    for key, value in merge_content.items():
        if key.startswith(("prepend-", "append-")):
            continue
        data[key] = value
    if merge_content.get("append-proxy-groups") or merge_content.get("prepend-proxy-groups"):
        data["proxy-groups"] = groups + list(merge_content.get("append-proxy-groups") or []) \
            + list(merge_content.get("prepend-proxy-groups") or [])
    return data


def writeback_preview() -> dict:
    """分级分组方案 + diff 预览；只读不动文件。"""
    cfg = load_config()
    verge = Path(cfg["verge_dir"])
    if not verge.is_dir():
        raise ValueError(f"Clash Verge 数据目录不存在: {verge}")
    metadata = _load_yaml(verge / "profiles.yaml")
    current = metadata.get("current")
    if not current:
        raise ValueError("profiles.yaml 缺少 current（激活配置）")
    groups, checked = _wb_build_groups(verge, current)
    original_text = _wb_read_merge_text(cfg)
    try:
        existing = yaml.safe_load(original_text) or {} if original_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Merge.yaml 不是有效 YAML（{type(exc).__name__}），拒绝写回，请先在 Verge 里修复")
    ours, others = _wb_split_managed(existing)
    if others:
        raise ValueError("Merge.yaml 已存在别人配置的 append-proxy-groups，工具箱不做文本补丁，"
                         "请先人工合并后再用")
    return {
        "merge_path": str(_wb_merge_path(cfg)),
        "active_profile": f"{current}.yaml",
        "checked_nodes": checked,
        "included_nodes": sum(len(v) for v in groups.values()),
        "groups": {name: {"count": len(members), "members": members[:12],
                          "truncated": max(0, len(members) - 12)}
                   for name, members in groups.items()},
        "existing_managed_groups": len(ours),
        "preview_yaml": _wb_groups_block(groups),
        "notes": [
            "组员只引用当前激活配置里真实存在的节点名；切换激活配置或订阅更新后节点可能缺失，需重新生成写回。",
            "生效：写回成功后到 Clash Verge 对当前订阅点「重新激活」（或重启 Verge）。",
            "三个组都是 select 组，默认不参与分流规则，只在 Verge 面板手动切换时用。",
            "写回流程：先备份 Merge.yaml（同目录时间戳副本）→ 组合配置过 mihomo -t → 通过才落盘。",
        ],
    }


def writeback_apply() -> dict:
    """确认后的落盘：备份 → 组合配置过 mihomo -t → 通过才原子写入 Merge.yaml。"""
    cfg = load_config()
    verge = Path(cfg["verge_dir"])
    if not verge.is_dir():
        raise ValueError(f"Clash Verge 数据目录不存在: {verge}")
    metadata = _load_yaml(verge / "profiles.yaml")
    current = metadata.get("current")
    if not current:
        raise ValueError("profiles.yaml 缺少 current（激活配置）")
    groups, _checked = _wb_build_groups(verge, current)
    if not any(groups.values()):
        raise ValueError("没有可写回的分组（还没有检测结果，或激活配置里没有命中节点名）")
    original_text = _wb_read_merge_text(cfg)
    try:
        existing = yaml.safe_load(original_text) or {} if original_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Merge.yaml 不是有效 YAML（{type(exc).__name__}），拒绝写回")
    _ours, others = _wb_split_managed(existing)
    if others:
        raise ValueError("Merge.yaml 已存在别人配置的 append-proxy-groups，拒绝写回")
    final_text = _wb_compose_text(original_text, groups)
    try:
        merge_content = yaml.safe_load(final_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"补丁后 Merge 内容解析失败（{type(exc).__name__}），未写入")
    core = _find_core()
    if core is None:
        raise ValueError("找不到 Mihomo 内核，无法做写前校验")
    combined = _wb_combined_config(verge, current, merge_content)
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    test_path = PROBE_DIR / "wb-test.yaml"
    test_path.write_text(yaml.safe_dump(combined, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
    try:
        test = subprocess.run([str(core), "-t", "-d", str(verge), "-f", str(test_path)],
                              capture_output=True, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        try:
            test_path.unlink()
        except OSError:
            pass
    if test.returncode != 0:
        # 不回显内核诊断（可能带节点参数）
        raise ValueError("写前校验未通过（内核拒绝组合配置），未写入任何文件；"
                         "常见原因是激活配置里有节点参数不合法，可先跑一轮配置体检")
    backup_path = _wb_merge_path(cfg).with_name(
        f"Merge.yaml.bak-cn-audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    if original_text:
        shutil.copy2(_wb_merge_path(cfg), backup_path)
    _atomic_write(_wb_merge_path(cfg), final_text)
    _log(f"分级写回落盘: groups={{{', '.join(f'{k}:{len(v)}' for k, v in groups.items())}}} "
         f"backup={backup_path.name}")
    return {"ok": True, "backup": str(backup_path) if original_text else None,
            "groups": {name: len(members) for name, members in groups.items()},
            "hint": "已写入 Merge.yaml。到 Clash Verge 对当前订阅点「重新激活」后生效。"}


def writeback_remove() -> dict:
    """只移除工具箱管理的三个分组（整段文本块摘除），其余内容原样保留；先备份。"""
    cfg = load_config()
    original_text = _wb_read_merge_text(cfg)
    if WB_BEGIN not in original_text:
        return {"ok": True, "note": "Merge.yaml 里没有工具箱的写回段"}
    backup_path = _wb_merge_path(cfg).with_name(
        f"Merge.yaml.bak-cn-audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(_wb_merge_path(cfg), backup_path)
    pattern = re.compile(r"\n?" + re.escape(WB_BEGIN) + r".*?" + re.escape(WB_END) + r"\n?", re.S)
    new_text = pattern.sub("\n", original_text)
    _atomic_write(_wb_merge_path(cfg), new_text)
    _log(f"分级写回移除: backup={backup_path.name}")
    return {"ok": True, "backup": str(backup_path),
            "hint": "已从 Merge.yaml 摘除管理段。到 Clash Verge 点「重新激活」后生效。"}


# ---------- 报告与配置导出（对齐当前激活「安全节点配置」的分组体系） ----------
#
# 产物都落在 BASE/clash_node_reports/（滚动保留最近 REPORTS_KEEP 份）：
#   report = Markdown 检测报告，脱敏（无凭据、无完整 IP），按激活配置的分组体系给
#            各组建议名单，末尾附「AI 执行指引」——AI 拿报告即可直接改激活配置；
#   config = 按检测结果重排组员的完整 Clash 配置（骨架/凭据/providers 照搬激活配置），
#            可直接导入 Verge。该文件含节点凭据，只留本机，勿外传。
# 组员重排口径（报告与配置共用同一份 _suggested_groups，绝不相左）：
#   合格（风险分低在前）→ 观察 → 未检测/查询失败（保留原相对顺序）；不合格剔除；
#   国家组追加「同国合格但漏在组外」的静态节点；provider 节点准入靠 filter，不进静态名单。

REPORTS_DIR = BASE / "clash_node_reports"
REPORTS_KEEP = 20                     # 报告/配置合计滚动保留份数
AUTO_GROUP_MAX = 30                   # 自动选择类组建议名单上限
AUTO_GROUP_MAX_DELAY = 1500           # 自动选择追加节点的延迟上限（ms）


def _active_profile() -> tuple[Path, dict]:
    """当前激活配置：(路径, 解析后 dict)。报告与导出共用。"""
    cfg = load_config()
    verge = Path(cfg["verge_dir"])
    if not verge.is_dir():
        raise ValueError(f"Clash Verge 数据目录不存在: {verge}")
    metadata = _load_yaml(verge / "profiles.yaml")
    current = metadata.get("current")
    if not current:
        raise ValueError("profiles.yaml 缺少 current（激活配置）")
    path = verge / "profiles" / f"{current}.yaml"
    if not path.exists():
        raise ValueError(f"激活配置不存在: {path.name}")
    return path, _load_yaml(path)


def _active_display_name(path: Path) -> str:
    """激活配置的展示名（profiles.yaml items 里 name 字段，local 配置有）。"""
    cfg = load_config()
    try:
        metadata = _load_yaml(Path(cfg["verge_dir"]) / "profiles.yaml")
    except Exception:                          # noqa: BLE001
        return path.name
    for item in metadata.get("items") or []:
        if isinstance(item, dict) and item.get("file") == path.name:
            return str(item.get("name") or path.name)
    return path.name


def _group_role(name: str) -> tuple[str, str | None]:
    """组名 → (角色, 国家码)。对齐当前安全节点配置的组名体系：
    双绿/四绿/自动/敏感按关键词，其余按唯一国名/旗标命中归到国家组。"""
    if "双绿" in name:
        return "dual", None
    if "四绿" in name:
        return "quad", None
    if "自动" in name:
        return "auto", None
    if "敏感" in name:
        return "sensitive", None
    hits = set()
    for code, terms in _COUNTRY_ALIASES.items():
        if any(term in name for term in terms):
            hits.add(code)
    if len(hits) == 1:
        return "country", hits.pop()
    return "other", None


def _pub_rank(pub: dict) -> tuple:
    """建议名单排序键：合格在前，组内风险分低在前，再按延迟。"""
    fraud = pub.get("fraud_score")
    fraud_v = fraud if isinstance(fraud, (int, float)) and not isinstance(fraud, bool) else 999
    delay = pub.get("delay_ms")
    delay_v = delay if isinstance(delay, int) and not isinstance(delay, bool) else 99999
    order = {"合格": 0, "观察": 1}.get(pub.get("classification") or "", 2)
    return (order, fraud_v, delay_v)


def _report_rows() -> tuple[list[dict], dict]:
    """(已检测节点的 public 行, counts)。同名节点取首个（收集层已按指纹去重，
    同名不同参时结果行按 probe 编号各自存在，报告按展示名聚合取第一条）。"""
    cfg = load_config()
    by_name: dict[str, dict] = {}
    for row in list(_RESULTS.values()):
        if not row.get("checked_at"):
            continue
        pub = _public_row(row, cfg)
        name = pub.get("display_name") or ""
        if name and name not in by_name:
            by_name[name] = pub
    return list(by_name.values()), _counts()


def _bad_reasons(pub: dict, cfg: dict) -> list[str]:
    reasons = []
    fraud = pub.get("fraud_score")
    if isinstance(fraud, (int, float)) and not isinstance(fraud, bool) \
            and fraud > cfg["bad_threshold"]:
        reasons.append(f"风险分 {fraud} > {cfg['bad_threshold']}")
    if pub.get("is_broadcast") is True:
        reasons.append("广播 IP")
    if pub.get("label_mismatch") is True:
        reasons.append(f"标称 {pub.get('expected_country') or '未知'} "
                       f"实为 {pub.get('country_code') or '未知'}")
    return reasons


def _md_cell(text) -> str:
    """Markdown 表格单元格转义：节点名里自带 | 会把列撑爆。"""
    return str(text).replace("|", "\\|")


def _suggested_groups(active: dict) -> dict[str, dict]:
    """对激活配置每个 proxy-group 产出建议组员名单（报告与导出配置共用口径）。

    返回 {组名: {"role","country","members","removed","appended"}}。
    组员重排：合格→观察→未检测（含查询失败，保留原相对顺序）；不合格剔除。
    非 other 组追加漏在组外的合格静态节点（provider 节点名不在 proxies 段，
    静态引用会失效，故绝不追加；其准入调整靠报告里的 provider filter 建议）。"""
    pubs, _counts = _report_rows()
    by_name = {p["display_name"]: p for p in pubs}
    static_names = set()
    for proxy in active.get("proxies") or []:
        if isinstance(proxy, dict) and proxy.get("name"):
            static_names.add(str(proxy["name"]))

    suggestions: dict[str, dict] = {}
    for group in active.get("proxy-groups") or []:
        if not isinstance(group, dict) or not group.get("name") \
                or not isinstance(group.get("proxies"), list):
            continue
        gname = str(group["name"])
        role, country = _group_role(gname)
        buckets: dict[str, list] = {"合格": [], "观察": []}
        keep_original: list[str] = []
        removed: list[str] = []
        for member in (str(x) for x in group["proxies"] if isinstance(x, str)):
            pub = by_name.get(member)
            if pub is None:
                keep_original.append(member)          # 组引用/未检测节点，原样保留
            elif pub["classification"] in buckets:
                buckets[pub["classification"]].append(pub)
            elif pub["classification"] == "不合格":
                removed.append(member)
            else:
                keep_original.append(member)          # 查询失败，不擅动
        members = [p["display_name"] for p in sorted(buckets["合格"], key=_pub_rank)] \
            + [p["display_name"] for p in sorted(buckets["观察"], key=_pub_rank)] \
            + keep_original
        appended: list[str] = []
        if role != "other":
            for pub in sorted(pubs, key=_pub_rank):
                if pub["classification"] != "合格":
                    continue
                dn = pub["display_name"]
                if dn in members or dn not in static_names:
                    continue
                if role == "dual" and not pub.get("dual_green"):
                    continue
                if role == "quad" and not pub.get("four_green"):
                    continue
                if role == "sensitive" and not (pub.get("is_residential") is True
                                                or pub.get("is_native") is True):
                    continue
                if role == "country" and pub.get("country_code") != country:
                    continue
                if role == "auto":
                    delay = pub.get("delay_ms")
                    if not (isinstance(delay, int) and delay <= AUTO_GROUP_MAX_DELAY):
                        continue
                    if len(members) >= AUTO_GROUP_MAX:
                        continue
                members.append(dn)
                appended.append(dn)
        suggestions[gname] = {"role": role, "country": country, "members": members,
                              "removed": removed, "appended": appended}
    return suggestions


def _provider_ok_nodes() -> dict[str, list[str]]:
    """provider 来源节点中合格者的展示名（静态名单之外的准入靠 provider filter）。"""
    pubs, _counts = _report_rows()
    out: dict[str, list[str]] = {}
    for pub in sorted(pubs, key=_pub_rank):
        if pub["classification"] != "合格":
            continue
        for source in pub.get("sources") or []:
            if str(source).startswith("provider:"):
                out.setdefault(str(source)[len("provider:"):], []).append(pub["display_name"])
                break
    return out


def _reports_cleanup() -> None:
    try:
        for old in sorted(REPORTS_DIR.iterdir())[:-REPORTS_KEEP]:
            if old.is_file():
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _checked_summary(pubs: list[dict]) -> dict:
    """已检测行的按分级计数（与 _counts 口径一致，但只数有 checked_at 的行）。"""
    out = {"合格": 0, "观察": 0, "不合格": 0, "查询失败": 0}
    for pub in pubs:
        out[pub["classification"]] = out.get(pub["classification"], 0) + 1
    return out


def build_report() -> tuple[str, str]:
    """生成 Markdown 检测报告并落盘 clash_node_reports/，返回 (文件名, 正文)。
    脱敏红线：无凭据、无完整出口 IP（沿用结果缓存的脱敏字段）。"""
    cfg = load_config()
    active_path, active = _active_profile()
    pubs, counts = _report_rows()
    if not pubs:
        raise ValueError("还没有检测结果，请先跑一轮检测")
    suggestions = _suggested_groups(active)
    provider_ok = _provider_ok_nodes()
    core = _find_core()
    verge_dir = cfg["verge_dir"]

    lines: list[str] = []
    add = lines.append
    add(f"# Clash 节点检测报告")
    add("")
    add(f"- 报告生成：{_now_iso()}")
    add(f"- 当前激活配置：{_active_display_name(active_path)}（`{active_path}`）")
    add(f"- 已检测节点：{len(pubs)} 个（检测范围与订阅清单见工具箱节点检测页）")
    add(f"- 分级口径：fraudScore ≤ {cfg['ok_threshold']} 且非广播且名实相符 → 合格；"
        f"fraudScore > {cfg['bad_threshold']} / 广播出口 / 名实不符 → 不合格；其余 → 观察。"
        f"双绿 = 住宅 + 原生；四绿 = 双绿 + IPPure < {cfg['dual_green_threshold']} + CF < "
        f"{cfg['cf_threshold']}（CF 检查未实现，四绿当前恒不可达）。")
    add(f"- 建议名单排序：合格（风险分低在前）→ 观察 → 未检测保留；不合格剔除。")
    add("")
    add("## 总览")
    add("")
    summary = _checked_summary(pubs)
    add("| 合格 | 观察 | 不合格 | 查询失败 | 双绿 | 名实不符 |")
    add("|---|---|---|---|---|---|")
    add(f"| {summary['合格']} | {summary['观察']} | {summary['不合格']} | "
        f"{summary['查询失败']} | {counts.get('dual_green', 0)} | {counts.get('mismatch', 0)} |")
    add("")

    add("## 分组建议（按当前激活配置的分组体系）")
    add("")
    add("> 节点名必须**原样**使用（含 emoji、空格、前后缀）。未检测/查询失败的现有组员保留原位，不做臆断。")
    add("")
    for gname, sug in suggestions.items():
        if sug["role"] == "other" and not sug["removed"]:
            continue                        # 通用组（日常/节点选择等）只报剔除，不给名单
        title = {"dual": "（住宅+原生）", "quad": "（四绿）", "auto": "（url-test 自动测速）",
                 "sensitive": "（住宅/原生优先）", "country": "（同国合格自动补位）",
                 "other": ""}[sug["role"]]
        add(f"### {gname} {title}".rstrip())
        add("")
        add("、".join(f"**{m}**" if m in sug["appended"] else m for m in sug["members"])
            or "（建议清空，无保留组员）")
        if sug["appended"]:
            add("")
            add(f"- 加粗为建议新增（合格且漏在组外的静态节点，共 {len(sug['appended'])} 个）。")
        add("")
    removed_all = {g: s for g, s in suggestions.items() if s["removed"]}
    if removed_all:
        add("### 建议剔除（现役组员中被判不合格）")
        add("")
        add("| 组 | 节点 | 原因 |")
        add("|---|---|---|")
        for gname, sug in removed_all.items():
            for member in sug["removed"]:
                pub = next((p for p in pubs if p["display_name"] == member), None)
                reason = "；".join(_bad_reasons(pub, cfg)) if pub else "—"
                add(f"| {_md_cell(gname)} | {_md_cell(member)} | {_md_cell(reason)} |")
        add("")

    if provider_ok:
        add("### provider 节点准入（静态名单之外，靠 filter 正则）")
        add("")
        add("以下 provider 来源节点判为合格，但组里静态引用会失效（节点定义在 provider 缓存），"
            "调整各组 `use` 对应 provider 的 `filter` 正则来放行：")
        add("")
        add("| provider | 合格节点名 |")
        add("|---|---|")
        for pname, names in provider_ok.items():
            add(f"| {_md_cell(pname)} | {_md_cell('、'.join(names))} |")
        add("")

    bads = [p for p in pubs if p["classification"] == "不合格"]
    add("## 不合格明细")
    add("")
    if bads:
        add("| 节点 | 出口 | 风险分 | 脱敏 IP | 延迟 | 原因 |")
        add("|---|---|---|---|---|---|")
        for pub in sorted(bads, key=_pub_rank):
            add(f"| {_md_cell(pub['display_name'])} | {pub.get('country_code') or '—'} | "
                f"{pub.get('fraud_score') if pub.get('fraud_score') is not None else '—'} | "
                f"{pub.get('ip_redacted') or '—'} | "
                f"{pub.get('delay_ms') if pub.get('delay_ms') is not None else '—'} | "
                f"{_md_cell('；'.join(_bad_reasons(pub, cfg)) or '—')} |")
    else:
        add("（无）")
    add("")

    watch = sorted((p for p in pubs if p["classification"] == "观察"), key=_pub_rank)
    add("## 观察名单")
    add("")
    if watch:
        by_cc: dict[str, list[str]] = {}
        for pub in watch:
            by_cc.setdefault(pub.get("country_code") or "未知", []).append(pub["display_name"])
        for key, names in by_cc.items():
            add(f"- **{key}**：{_md_cell('、'.join(names))}")
    else:
        add("（无）")
    add("")

    fails = [p for p in pubs if p["classification"] == "查询失败"]
    add("## 查询失败名单")
    add("")
    if fails:
        by_err: dict[str, list[str]] = {}
        for pub in sorted(fails, key=lambda p: p["display_name"]):
            err = (pub.get("ippure_error") or pub.get("ipapi_error")
                   or pub.get("ping0_error") or "未知")
            by_err.setdefault(str(err), []).append(pub["display_name"])
        for err, names in by_err.items():
            add(f"- **{err}**（{len(names)} 个）：{_md_cell('、'.join(names))}")
        add("")
        add("查询失败不参与分级，不剔除、不重排；下次检测会自动重试。")
    else:
        add("（无）")
    add("")

    add("## AI 执行指引")
    add("")
    add("拿本报告执行节点配置更新时，按以下规则操作：")
    add("")
    add(f"1. 目标文件：`{active_path}`（Verge 当前激活配置）。先在同目录做时间戳备份，"
        f"绝不覆盖前无备份。")
    add("2. 只修改 `proxy-groups` 里上文的组员名单（`proxies:` 列表）；组里的 `use`、"
        "`filter`、`exclude-filter`、`url`、`interval` 等字段保留不动。")
    add("3. 顶层 `proxies:` 凭据段与 `proxy-providers:` 禁止改动；节点名原样引用。")
    add("4. 组员名单来源：本文「分组建议」各节；不在建议名单里的现有组员不要自行增删。")
    add("5. 改完用内核校验（PowerShell，路径照实际安装位置）：")
    add(f"   `& \"{(core or Path(cfg['core_path']))}\" -t -d \"{verge_dir}\" "
        f"-f \"{active_path}\"`，退出码 0 才算通过。")
    add("6. 校验通过后在 Clash Verge 对该配置点「重新激活」（或重启 Verge）生效。")
    add("7. provider 来源节点的放行不进静态名单，只调整对应 provider 的 `filter` 正则，"
        "节点名参照上文 provider 表（注意正则转义 `.`、`|` 等）。")
    add("8. 本报告已脱敏（无凭据、无完整 IP），可整份交给 AI；但生成的配置导出文件"
        "（clash_node_reports/ 下的 *.yaml）含节点凭据，**不要**把那个文件发给 AI 或上传。")
    add("")
    add("### 组员名单补丁（供直接替换对应组的 proxies 列表）")
    add("")
    add("```yaml")
    for gname, sug in suggestions.items():
        if sug["role"] == "other" and not sug["removed"]:
            continue
        add(f"# ===== 组：{gname} =====")
        add(f"- name: {gname}")
        add("  proxies: []" if not sug["members"] else "  proxies:")
        for m in sug["members"]:
            add(f"  - {m}")
    add("```")
    add("")
    add("---")
    add("报告由小宇工具箱节点检测自动生成；分级阈值可在节点检测页「设置」调整后重新导出。")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"clash-node-report-{stamp}.md"
    _atomic_write(REPORTS_DIR / filename, "\n".join(lines) + "\n")
    _reports_cleanup()
    _log(f"检测报告落盘: {filename} nodes={len(pubs)}")
    return filename, "\n".join(lines) + "\n"


def export_config() -> tuple[str, str]:
    """按检测结果重排组员，产出完整 Clash 配置（骨架/凭据/providers 照搬激活配置），
    落盘 clash_node_reports/，返回 (文件名, yaml 文本)。注意：文本含节点凭据。"""
    active_path, active = _active_profile()
    suggestions = _suggested_groups(active)
    if not any(s["removed"] or s["appended"] for s in suggestions.values()):
        raise ValueError("激活配置的各组组员按当前检测结果没有需要变动的地方"
                         "（组员里没有不合格节点，也没有漏在组外的合格静态节点），"
                         "无需导出；可先看「导出报告」里的 provider filter 建议")
    out = dict(active)
    new_groups = []
    changed = {}
    for group in active.get("proxy-groups") or []:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        gname = str(group.get("name") or "")
        sug = suggestions.get(gname)
        g2 = dict(group)
        if sug and isinstance(group.get("proxies"), list) \
                and (sug["removed"] or sug["appended"]):
            g2["proxies"] = list(sug["members"])
            changed[gname] = {"removed": len(sug["removed"]), "appended": len(sug["appended"])}
        new_groups.append(g2)
    out["proxy-groups"] = new_groups
    text = yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=4096)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"clash-config-{stamp}.yaml"
    _atomic_write(REPORTS_DIR / filename, text)
    _reports_cleanup()
    _log(f"配置导出落盘: {filename} groups={changed}")
    return filename, text


# ---------- 模块初始化 ----------

_load_cache()
# 2026-10-14：_cleanup_orphan() 不再在此处（模块导入时）执行——
# 任何外部 python 进程 import 本模块都会重新执行模块级代码，而清扫会按 runtime.json
# 杀掉"正在被扫描使用"的内核 → 扫描从此全灭（selection_error，已实测复刻）。
# 清扫已移至「扫描/重测启动前」执行（_scan_work / retest 入口），语义即"清上个进程残留"。
