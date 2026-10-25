# -*- coding: utf-8 -*-
"""clash_inspector.py - Clash 实时 IP、目标网站链路溯源与 DNS 诊断模块

解耦设计：
- 直接与本机运行的 Clash Verge Rev / Mihomo 交互（支持 Windows Named Pipe 与 Mixed-port）。
- 独立于 Clash Verge 安装目录与更新，Clash 升级不影响本模块。
- 提供：
  1. 当前代理出口 IPPure (欺诈分/住宅属性) + Ping0 纯净度检测 + 多源画像（Cloudflare 视角 / ip-api 风控标记 / ipinfo / IP.SB）+ 本地直连 IP 对比。
  2. 任意目标域名 (如 claude.ai) 规则匹配、代理链追踪与出口溯源。
  3. DNS 解析探测、Fake-IP 验证与 DNS 泄露分析。
  4. 任意 IP 综合画像查询（IPPure 官方 API：ASN/AS域名/IP范围/人机流量比/系数 + 多源交叉 + 反向 DNS）。
  5. Cloudflare 云防风险指数（ippure 云防检测页，浏览器模拟；源自 Cloudflare Bot Management）。
"""

import os
import re
import json
import glob
import time
import socket
import hmac
import hashlib
import shutil
import ssl
import subprocess
import tempfile
import ipaddress
import threading
import http.client
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List

try:
    import win32file
    import pywintypes
    HAS_PYWIN32 = True
except ImportError:
    HAS_PYWIN32 = False

# 探测源常量
IPPURE_URL = "https://my.ippure.com/v1/info"
PING0_GEO_URL = "https://ping0.cc/geo"
IPAPI_FULL_URL = "http://ip-api.com/json/?fields=status,message,country,countryCode,regionName,city,query,as,asname,isp,org,mobile,proxy,hosting"
IPINFO_URL = "https://ipinfo.io/json"
IPSB_GEO_URL = "https://api.ip.sb/geoip"
# Cloudflare 视角：1.1.1.1 主源 → www.cloudflare.com 兜底（2026-10-14 实测两条均可直取）
CF_TRACE_URLS = ("https://1.1.1.1/cdn-cgi/trace", "https://www.cloudflare.com/cdn-cgi/trace")
# IPPure 官方 API（api.ippure.com，x-k/x-t 签名鉴权；见 _IPPureAPIClient）
IPPURE_API_HOST = "api.ippure.com"
IPPURE_API_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
# Cloudflare 云防检测页（浏览器模拟抓取；数据源 = Cloudflare Bot Management）
IPPURE_CF_PAGE_URL = "https://ippure.com/cloudflare"
CF_SCORE_TTL = 30 * 60      # 云防检测结果内存缓存（秒）
CF_RUN_TIMEOUT = 120        # 单次无头浏览器检测超时（秒）
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
IPWHO_BASE = "https://ipwho.is/"
EDNS_LEAK_URL = "http://edns.ip-api.com/json"

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {
    "current_ip": None,
    "current_ip_ts": 0,
    "dns_status": None,
    "dns_status_ts": 0,
}
_CF_CACHE: Dict[str, Any] = {}
_CF_LOCKS: Dict[str, threading.Lock] = {"proxy": threading.Lock(), "direct": threading.Lock()}
CACHE_TTL = 20.0  # 20秒缓存，避免频繁刷接口被限流


# ---------- Mihomo Named Pipe 与 配置嗅探 ----------

def _get_verge_paths() -> Tuple[Optional[str], Optional[str], int]:
    """返回 (pipe_path, secret, mixed_port)"""
    appdata = os.environ.get("APPDATA") or ""
    verge_yaml = os.path.join(appdata, "io.github.clash-verge-rev.clash-verge-rev", "clash-verge.yaml")
    
    pipe_path = None
    secret = ""
    mixed_port = 7890
    
    if not os.path.isfile(verge_yaml):
        return None, None, mixed_port
        
    try:
        with open(verge_yaml, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
        port_m = re.search(r"mixed-port:\s*(\d+)", content)
        if port_m:
            mixed_port = int(port_m.group(1))
            
        secret_m = re.search(r"secret:\s*(.+)", content)
        if secret_m:
            secret = secret_m.group(1).strip().strip("'\"")
            if secret in ("null", "~"):
                secret = ""
                
        pipe_m = re.search(r"external-controller-pipe:\s*(.+)", content)
        if pipe_m:
            pipe_line = pipe_m.group(1).strip()
            pipe_hash_m = re.search(r"[a-f0-9]{64}", pipe_line)
            if pipe_hash_m:
                pipe_hash = pipe_hash_m.group(0)
                pipes = glob.glob(r"\\.\pipe\*")
                matches = [p for p in pipes if pipe_hash in p]
                if matches:
                    pipe_path = matches[0]
    except Exception:
        pass
        
    return pipe_path, secret, mixed_port


def _unchunk(data: bytes) -> bytes:
    """解析 HTTP chunked 编码流"""
    result = bytearray()
    while data:
        idx = data.find(b"\r\n")
        if idx == -1:
            break
        size_str = data[:idx].split(b";")[0].strip()
        if not size_str:
            data = data[idx + 2:]
            continue
        try:
            chunk_size = int(size_str, 16)
        except ValueError:
            break
        if chunk_size == 0:
            break
        chunk = data[idx + 2: idx + 2 + chunk_size]
        result.extend(chunk)
        data = data[idx + 2 + chunk_size + 2:]
    return bytes(result)


def mihomo_api_call(endpoint: str, method: str = "GET") -> Tuple[int, Any]:
    """通过 Windows Named Pipe 访问 Mihomo 控制器 API"""
    if not HAS_PYWIN32:
        return 500, {"error": "pywin32 未安装"}
        
    pipe_path, secret, _ = _get_verge_paths()
    if not pipe_path:
        return 503, {"error": "未检测到活跃的 Clash Verge Mihomo 命名管道，请确认 Clash 正在运行"}
        
    try:
        handle = win32file.CreateFile(
            pipe_path,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None
        )
        
        auth_header = f"Authorization: Bearer {secret}\r\n" if secret else ""
        req = f"{method} {endpoint} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_header}Connection: close\r\n\r\n"
        win32file.WriteFile(handle, req.encode("utf-8"))
        
        resp = b""
        while True:
            try:
                hr, data = win32file.ReadFile(handle, 8192)
                if not data:
                    break
                resp += data
            except pywintypes.error as ex:
                if ex.winerror == 109:  # ERROR_BROKEN_PIPE: 对端正常发送完毕关闭连接
                    break
                raise
        win32file.CloseHandle(handle)
        
        parts = resp.split(b"\r\n\r\n", 1)
        headers = parts[0].decode("utf-8", errors="ignore")
        body_bytes = parts[1] if len(parts) > 1 else b""
        
        status = 200
        status_line = headers.splitlines()[0] if headers else ""
        sm = re.search(r"HTTP/\S+\s+(\d+)", status_line)
        if sm:
            status = int(sm.group(1))
            
        if b"Transfer-Encoding: chunked" in parts[0] or b"transfer-encoding: chunked" in parts[0]:
            body_bytes = _unchunk(body_bytes)
            
        try:
            return status, json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return status, body_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        return 500, {"error": f"管道调用失败: {e}"}


# ---------- 代理与直连网络请求 ----------

def _fetch_proxied(url: str, port: int, timeout: float = 6.0) -> Tuple[Optional[bytes], Optional[str]]:
    proxy = f"http://127.0.0.1:{port}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Connection": "close"
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP_{error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return None, type(error).__name__


def _fetch_direct(url: str, timeout: float = 5.0) -> Tuple[Optional[bytes], Optional[str]]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Connection": "close"
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 空 handler 绕过本地代理
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP_{error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return None, type(error).__name__


def _guard_pair(job) -> Tuple[Optional[bytes], Optional[str]]:
    """并发任务兜底：把任何异常归一为 (None, 异常名)，避免单点失败拖垮整批探测"""
    try:
        return job()
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


def _retry_once(job) -> Tuple[Optional[bytes], Optional[str]]:
    """首拉无数据时稍候重试一次（2026-10-14 实测并发突发下偶发单点失败≈1/5），失败仍按原错误上报"""
    data, err = _guard_pair(job)
    if data is None:
        time.sleep(0.5)
        data, err = _guard_pair(job)
    return data, err


def _parse_cf_trace(raw: bytes) -> Dict[str, Any]:
    """解析 Cloudflare cdn-cgi/trace 的 key=value 文本行"""
    data: Dict[str, Any] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def _fetch_cf_trace(port: Optional[int]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Cloudflare 视角（1.1.1.1 主源 → www.cloudflare.com 兜底）；port=None 走直连"""
    last_err = None
    for url in CF_TRACE_URLS:
        raw, err = (_fetch_proxied(url, port, timeout=5.0) if port else _fetch_direct(url, timeout=5.0))
        if raw:
            trace = _parse_cf_trace(raw)
            if trace.get("ip"):
                return trace, None
            last_err = "empty_trace"
        else:
            last_err = err
    return None, last_err


def _parse_ipapi(raw: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """解析 ip-api.com JSON；非 success 归一为错误信息"""
    d = json.loads(raw.decode("utf-8", errors="replace"))
    if d.get("status") != "success":
        return None, str(d.get("message") or "query_failed")
    return {
        "ip": d.get("query"),
        "country": d.get("country"),
        "country_code": d.get("countryCode"),
        "region": d.get("regionName"),
        "city": d.get("city"),
        "isp": d.get("isp"),
        "org": d.get("org"),
        "asn": d.get("as"),
        "asname": d.get("asname"),
        "mobile": d.get("mobile"),
        "proxy": d.get("proxy"),
        "hosting": d.get("hosting"),
    }, None


def _new_ext_blocks() -> Dict[str, Dict[str, Any]]:
    """出口卡片的四个多源信息块初始模板（未查到/失败均以 status 表达）"""
    return {
        "cloudflare": {"status": "pending", "ip": None, "loc": None, "colo": None,
                       "warp": None, "http": None, "tls": None, "error": None},
        "ipapi": {"status": "pending", "ip": None, "country": None, "country_code": None,
                  "region": None, "city": None, "isp": None, "org": None, "asn": None,
                  "asname": None, "mobile": None, "proxy": None, "hosting": None, "error": None},
        "ipinfo": {"status": "pending", "ip": None, "city": None, "region": None,
                   "country": None, "org": None, "timezone": None, "error": None},
        "ipsb": {"status": "pending", "ip": None, "isp": None, "org": None, "asn": None,
                 "country": None, "country_code": None, "city": None, "error": None},
        "ipwho": {"status": "pending", "ip": None, "country": None, "country_code": None,
                  "region": None, "city": None, "isp": None, "org": None, "asn": None, "error": None},
    }


def _apply_ext_sources(side: Dict[str, Any], cf_res, ipinfo_res, ipsb_res, ipwho_res) -> None:
    """填充 Cloudflare / ipinfo / IP.SB / ipwho.is 四个扩展源（ip-api 因直连侧需复用解析，单独处理）"""
    # Cloudflare 视角
    cf, cf_err = cf_res
    if cf:
        side["cloudflare"].update({
            "status": "ok", "ip": cf.get("ip"), "loc": cf.get("loc"), "colo": cf.get("colo"),
            "warp": cf.get("warp"), "http": cf.get("http"), "tls": cf.get("tls"), "error": None,
        })
    else:
        side["cloudflare"].update({"status": "failed", "error": cf_err or "failed"})

    # ipinfo 交叉源
    raw, err = ipinfo_res
    if raw:
        try:
            d = json.loads(raw.decode("utf-8", errors="replace"))
            side["ipinfo"].update({
                "status": "ok", "ip": d.get("ip"), "city": d.get("city"), "region": d.get("region"),
                "country": d.get("country"), "org": d.get("org"), "timezone": d.get("timezone"), "error": None,
            })
        except Exception as e:  # noqa: BLE001
            side["ipinfo"].update({"status": "failed", "error": f"parse: {e}"})
    else:
        side["ipinfo"].update({"status": "failed", "error": err or "failed"})

    # IP.SB 交叉源
    raw, err = ipsb_res
    if raw:
        try:
            d = json.loads(raw.decode("utf-8", errors="replace"))
            side["ipsb"].update({
                "status": "ok", "ip": d.get("ip"), "isp": d.get("isp"), "org": d.get("organization"),
                "asn": d.get("asn"), "country": d.get("country"), "country_code": d.get("country_code"),
                "city": d.get("city"), "error": None,
            })
        except Exception as e:  # noqa: BLE001
            side["ipsb"].update({"status": "failed", "error": f"parse: {e}"})
    else:
        side["ipsb"].update({"status": "failed", "error": err or "failed"})

    # ipwho.is 交叉源
    raw, err = ipwho_res
    if raw:
        try:
            d = json.loads(raw.decode("utf-8", errors="replace"))
            if d.get("success") is False:
                side["ipwho"].update({"status": "failed", "error": str(d.get("message") or "query_failed")})
            else:
                conn = d.get("connection") or {}
                side["ipwho"].update({
                    "status": "ok", "ip": d.get("ip"), "country": d.get("country"),
                    "country_code": d.get("country_code"), "region": d.get("region"), "city": d.get("city"),
                    "isp": conn.get("isp"), "org": conn.get("org"), "asn": conn.get("asn"), "error": None,
                })
        except Exception as e:  # noqa: BLE001
            side["ipwho"].update({"status": "failed", "error": f"parse: {e}"})
    else:
        side["ipwho"].update({"status": "failed", "error": err or "failed"})


def _apply_ipapi_block(side: Dict[str, Any], raw: Optional[bytes], err: Optional[str]) -> Optional[Dict[str, Any]]:
    """填充 ip-api 块；成功时返回解析结果（直连侧复用其拿 isp/asn 旧字段）"""
    if not raw:
        side["ipapi"].update({"status": "failed", "error": err or "failed"})
        return None
    try:
        d, perr = _parse_ipapi(raw)
        if not d:
            side["ipapi"].update({"status": "failed", "error": perr or "failed"})
            return None
        side["ipapi"].update(d)
        side["ipapi"]["status"] = "ok"
        return d
    except Exception as e:  # noqa: BLE001
        side["ipapi"].update({"status": "failed", "error": f"parse: {e}"})
        return None


# ---------- IPPure 官方 API（x-k/x-t 签名）与 Cloudflare 云防检测 ----------

class _IPPureAPIClient:
    """api.ippure.com 签名客户端。

    鉴权流程（复刻 ippure 前端 security chunk，2026-10-14 逆向验证）：
    ① 先发一个无签名请求 bootstrap，从响应头取 x-k（会话密钥）与 x-t（服务器毫秒时间）；
    ② 同一 TCP/TLS 连接内，后续请求带 x-k + x-t = `{t}-{HMAC_SHA256(x-k, "GET-{url}--{t}")}`。
    **连接复用是校验的一部分**：换连接会让签名作废（服务端重新下发 x-k）。
    """

    def __init__(self, proxy_port: Optional[int] = None):
        self.host = IPPURE_API_HOST
        self.proxy_port = proxy_port
        self.conn: Optional[http.client.HTTPSConnection] = None
        self.xk: Optional[str] = None
        self.delta = 0

    def _connect(self) -> http.client.HTTPSConnection:
        ctx = ssl.create_default_context()
        if self.proxy_port:
            conn = http.client.HTTPSConnection("127.0.0.1", self.proxy_port, context=ctx, timeout=8)
            conn.set_tunnel(self.host, 443)
        else:
            conn = http.client.HTTPSConnection(self.host, 443, context=ctx, timeout=8)
        return conn

    def _req(self, path: str, extra: Optional[Dict[str, str]] = None) -> Tuple[Any, bytes]:
        headers = {
            "Host": self.host,
            "User-Agent": IPPURE_API_UA,
            "Origin": "https://ippure.com",
            "Referer": "https://ippure.com/",
            "Accept": "*/*",
        }
        if extra:
            headers.update(extra)
        if self.conn is None:
            self.conn = self._connect()
        self.conn.request("GET", path, headers=headers)
        resp = self.conn.getresponse()
        return resp, resp.read()

    def bootstrap(self) -> None:
        resp, _ = self._req("/api/info/ip-risk/8.8.8.8")
        self.xk = resp.getheader("x-k")
        server_t = resp.getheader("x-t")
        if not self.xk or not server_t:
            raise OSError("ippure bootstrap 失败：响应缺少 x-k/x-t")
        self.delta = int(server_t) - int(time.time() * 1000)

    def _signed(self, path: str) -> Tuple[Any, bytes]:
        t = int(time.time() * 1000) + self.delta
        msg = f"GET-https://{self.host}{path}--{t}"
        sig = hmac.new(self.xk.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return self._req(path, {"x-k": self.xk, "x-t": f"{t}-{sig}"})

    def get_json(self, path: str) -> Dict[str, Any]:
        if not self.xk:
            self.bootstrap()
        resp, body = self._signed(path)
        if resp.getheader("x-k"):  # 会话失效：重建后重试一次
            self.bootstrap()
            resp, body = self._signed(path)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"响应解析失败: {body[:60]!r}"}
        return data if isinstance(data, dict) else {"ok": False, "error": "响应格式异常"}

    def close(self) -> None:
        try:
            if self.conn:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def _ippure_reports(target_ips: List[str], proxy_port: Optional[int]) -> Dict[str, Dict[str, Any]]:
    """在一次复用连接里查询多个 IP 的 IPPure 报告（ip-basic / ip-risk / botclass）。

    直连失败时自动改走本地混合端口。返回 {ip: {basic, risk_score, botclass, error}}。
    """
    ips = [ip for ip in dict.fromkeys(target_ips) if ip]
    result: Dict[str, Dict[str, Any]] = {}
    if not ips:
        return result

    attempts: List[Optional[int]] = [None] + ([proxy_port] if proxy_port else [])
    last_err: Optional[str] = None
    for route in attempts:
        client = _IPPureAPIClient(proxy_port=route)
        try:
            client.bootstrap()
            filled: Dict[str, Dict[str, Any]] = {}
            for ip in ips:
                rep: Dict[str, Any] = {"basic": None, "risk_score": None, "botclass": None, "error": None}
                basic = client.get_json(f"/api/info/ip-basic/{ip}")
                if basic.get("ok"):
                    rep["basic"] = basic.get("data")
                else:
                    rep["error"] = str(basic.get("error") or "ip-basic 查询失败")
                risk = client.get_json(f"/api/info/ip-risk/{ip}")
                if risk.get("ok"):
                    rep["risk_score"] = (risk.get("data") or {}).get("risk_score")
                asn = ((rep["basic"] or {}).get("asn") or {}).get("number")
                if asn:
                    bc = client.get_json(f"/api/info/asn/botclass/{asn}")
                    if bc.get("ok"):
                        rep["botclass"] = bc.get("data")
                filled[ip] = rep
            client.close()
            return filled
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            client.close()
    for ip in ips:
        result[ip] = {"basic": None, "risk_score": None, "botclass": None, "error": last_err or "查询失败"}
    return result


def _reverse_dns(ip: str) -> Dict[str, Any]:
    """反向 DNS（PTR）查询"""
    try:
        name = socket.gethostbyaddr(ip)[0]
        return {"status": "ok", "hostname": name, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "hostname": None, "error": type(e).__name__}


def _map_ipinfo_block(d: Dict[str, Any]) -> Dict[str, Any]:
    return {"ip": d.get("ip"), "hostname": d.get("hostname"), "city": d.get("city"), "region": d.get("region"),
            "country": d.get("country"), "org": d.get("org"), "timezone": d.get("timezone")}


def _map_ipsb_block(d: Dict[str, Any]) -> Dict[str, Any]:
    return {"ip": d.get("ip"), "isp": d.get("isp"), "org": d.get("organization"), "asn": d.get("asn"),
            "country": d.get("country"), "country_code": d.get("country_code"), "city": d.get("city")}


def _map_ipwho_block(d: Dict[str, Any]) -> Dict[str, Any]:
    if d.get("success") is False:
        raise ValueError(str(d.get("message") or "query_failed"))
    conn = d.get("connection") or {}
    return {"ip": d.get("ip"), "country": d.get("country"), "country_code": d.get("country_code"),
            "region": d.get("region"), "city": d.get("city"), "isp": conn.get("isp"),
            "org": conn.get("org"), "asn": conn.get("asn")}


def _apply_json_source(block: Dict[str, Any], raw: Optional[bytes], err: Optional[str], mapper) -> None:
    """通用 JSON 源填充（用于任意 IP 查询页的独立数据块）"""
    if not raw:
        block.update({"status": "failed", "error": err or "failed"})
        return
    try:
        block.update(mapper(json.loads(raw.decode("utf-8", errors="replace"))))
        block["status"] = "ok"
        block["error"] = None
    except Exception as e:  # noqa: BLE001
        block.update({"status": "failed", "error": str(e) or "parse"})


def _get_api_keys() -> Tuple[str, str, str]:
    """读取已配置的 IPQualityScore、Ping0 与 ipapi.is API Key（支持 config 文件与环境变量）。"""
    ipqs_key = os.environ.get("IPQS_API_KEY", "").strip()
    ping0_key = os.environ.get("PING0_API_KEY", "").strip()
    ipapi_is_key = os.environ.get("IPAPI_IS_API_KEY", "").strip() or os.environ.get("IPAPI_API_KEY", "").strip()
    try:
        import sys
        candidates = [
            Path(__file__).resolve().parent / "clash_node_check.json",
            Path(sys.executable).resolve().parent / "clash_node_check.json",
        ]
        for cfg_path in candidates:
            if cfg_path.is_file():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if not ipqs_key:
                    ipqs_key = str(data.get("ipqs_api_key") or "").strip()
                if not ping0_key:
                    ping0_key = str(data.get("ping0_api_key") or "").strip()
                if not ipapi_is_key:
                    ipapi_is_key = str(data.get("ipapi_is_api_key") or data.get("ipapi_key") or "").strip()
                break
    except Exception:
        pass
    return ipqs_key, ping0_key, ipapi_is_key


def _fetch_scamalytics(ip: str, port: Optional[int]) -> Dict[str, Any]:
    """Scamalytics 风险与欺诈检测（Fraud Score 0-100、风险等级、机房/代理标记）。"""
    res: Dict[str, Any] = {
        "status": "pending",
        "score": None,
        "risk": None,
        "datacenter": None,
        "server": None,
        "residential": None,
        "tor": None,
        "vpn": None,
        "country": None,
        "country_code": None,
        "city": None,
        "error": None,
    }
    url = f"https://scamalytics.com/ip/{ip}"
    raw, err = _fetch_proxied(url, port, timeout=6.5) if port else (None, "no_port")
    if not raw:
        raw, err = _fetch_direct(url, timeout=6.5)
    if not raw:
        res["status"] = "failed"
        res["error"] = err or "请求失败"
        return res

    html = raw.decode("utf-8", errors="replace")
    m_score = re.search(r'Fraud Score:\s*(\d+)', html) or re.search(r'"score"\s*:\s*"(\d+)"', html)
    if not m_score:
        res["status"] = "failed"
        res["error"] = "未能解析到欺诈分"
        return res

    res["status"] = "ok"
    res["score"] = int(m_score.group(1))

    m_risk = re.search(r'(low|medium|high|very high)\s+risk', html, re.I)
    if m_risk:
        res["risk"] = m_risk.group(1).title()

    kv: Dict[str, str] = {}
    for m in re.finditer(r'<th>([^<]+)</th>\s*<td[^>]*>(?:<[^>]+>)*([^<]*)', html):
        k = m.group(1).strip()
        v = m.group(2).strip()
        if v or k not in kv:
            kv[k] = v

    def _bool_kv(k: str) -> Optional[bool]:
        v = kv.get(k, "").lower()
        return True if v == "yes" else (False if v == "no" else None)

    res["datacenter"] = _bool_kv("Datacenter")
    res["server"] = _bool_kv("Server")
    res["residential"] = _bool_kv("Residential Proxy")
    res["tor"] = _bool_kv("Tor Exit Node")
    res["vpn"] = _bool_kv("Anonymizing VPN")
    res["country"] = kv.get("Country Name")
    res["country_code"] = kv.get("Country Code")
    res["city"] = kv.get("City")
    return res


def _fetch_ipqs(ip: str, api_key: str, port: Optional[int]) -> Dict[str, Any]:
    """IPQualityScore 欺诈分与威胁情报（需 API Key，免费每月 1000 次）。"""
    res: Dict[str, Any] = {
        "status": "pending",
        "fraud_score": None,
        "proxy": None,
        "vpn": None,
        "tor": None,
        "recent_abuse": None,
        "bot_status": None,
        "country_code": None,
        "isp": None,
        "error": None,
    }
    if not api_key:
        res["status"] = "no_key"
        res["error"] = "选填：未配置 IPQualityScore Key（可在设置中配置）"
        return res

    url = f"https://www.ipqualityscore.com/api/json/ip/{api_key}/{ip}?strictness=1"
    raw, err = _fetch_proxied(url, port, timeout=6.0) if port else (None, "no_port")
    if not raw:
        raw, err = _fetch_direct(url, timeout=6.0)
    if not raw:
        res["status"] = "failed"
        res["error"] = err or "请求失败"
        return res

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if data.get("success"):
            res["status"] = "ok"
            res["fraud_score"] = int(data.get("fraud_score", 0))
            res["proxy"] = bool(data.get("proxy"))
            res["vpn"] = bool(data.get("vpn"))
            res["tor"] = bool(data.get("tor"))
            res["recent_abuse"] = bool(data.get("recent_abuse"))
            res["bot_status"] = bool(data.get("bot_status"))
            res["country_code"] = data.get("country_code")
            res["isp"] = data.get("ISP")
        else:
            res["status"] = "failed"
            res["error"] = data.get("message") or "API 请求未成功"
    except Exception as e:
        res["status"] = "failed"
        res["error"] = f"parse: {e}"
    return res


def _ping0_risk_level(score: int) -> str:
    if score < 15:
        return "极低风险"
    if score < 25:
        return "较低风险"
    if score < 40:
        return "一般风险"
    if score < 50:
        return "轻微风险"
    if score < 70:
        return "稍高风险"
    return "极度风险"


def _fetch_ping0(ip: str, api_key: str, port: Optional[int]) -> Dict[str, Any]:
    """Ping0 纯净度检测（风控分/原生IP/机房属性/位置/共享人数）。"""
    res: Dict[str, Any] = {
        "status": "pending",
        "score": None,
        "risk_level": None,
        "type": None,
        "native": None,
        "location": None,
        "usecount": None,
        "asn": None,
        "isp": None,
        "asndomain": None,
        "orgdomain": None,
        "error": None,
    }
    # 1. 若配置了 API Key，优先走官方 REST API
    if api_key:
        url = f"https://ping0.cc/apiloc/apikey({api_key})/ip({ip})"
        raw, _ = _fetch_proxied(url, port, timeout=6.0) if port else (None, "no_port")
        if not raw:
            raw, _ = _fetch_direct(url, timeout=6.0)
        if raw:
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(data, dict) and data.get("ip"):
                    res["status"] = "ok"
                    score_val = data.get("iprisk")
                    if score_val is not None:
                        res["score"] = int(score_val)
                        res["risk_level"] = _ping0_risk_level(res["score"])
                    res["location"] = data.get("location")
                    res["asn"] = data.get("asn")
                    res["isp"] = data.get("org") or data.get("asnname")
                    res["native"] = bool(data.get("isnative")) if data.get("isnative") is not None else None
                    res["type"] = "IDC机房" if data.get("isidc") else "家庭宽带"
                    res["usecount"] = data.get("usecount") or data.get("usershare") or data.get("sharecount")
                    return res
            except Exception:
                pass

    # 2. 网页解析兜底（模拟完整浏览器导航特征，代理/直连双通道自动互备，大幅提升免Key抓取成功率）
    url = f"https://ping0.cc/ip/{ip}"
    ping0_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://ping0.cc/",
        "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "close",
    }
    routes = [port, None] if port else [None]
    if None not in routes:
        routes.append(None)

    html = None
    last_err = None
    for p in routes:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{p}", "https": f"http://127.0.0.1:{p}"})) if p else urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            req = urllib.request.Request(url, headers=ping0_headers)
            with opener.open(req, timeout=6.0) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                if "AliyunCaptcha" not in text and len(text) > 5000:
                    html = text
                    break
                html = text
        except Exception as e:
            last_err = str(e)

    if not html:
        res["status"] = "failed"
        res["error"] = last_err or "请求失败"
        return res

    if "AliyunCaptcha" in html:
        res["status"] = "captcha"
        res["error"] = "Ping0 网页端触发人机验证（免Key受限，可在设置中配置 API Key）"
        return res

    try:
        m_risk = re.search(r'class="riskitem riskcurrent"[^>]*title="([^"]*)"[^>]*>.*?<span class="value">([^<]+)</span>', html, re.S)
        if m_risk:
            res["risk_level"] = m_risk.group(1).strip()
            val_txt = m_risk.group(2).replace("%", "").strip()
            try:
                res["score"] = int(val_txt)
            except ValueError:
                pass

        labels = [re.sub(r'<[^>]+>', '', l).strip() for l in re.findall(r'<span class="label[^"]*"[^>]*>(.*?)</span>', html)]
        if any("原生" in l for l in labels):
            res["native"] = True
        elif any("广播" in l for l in labels):
            res["native"] = False

        if any("IDC" in l for l in labels):
            res["type"] = "IDC机房"
        elif any("家庭" in l or "住宅" in l for l in labels):
            res["type"] = "家庭宽带"

        m_loc = re.search(r'window\.loc\s*=\s*[`\'"](.*?)[`\'"]', html, re.S)
        if m_loc:
            loc_val = m_loc.group(1).replace("&mdash;", "—").replace("&nbsp;", " ")
            res["location"] = re.sub(r'<[^>]+>', '', loc_val).strip()

        m_asn = re.search(r'window\.asndomain\s*=\s*[`\'"](.*?)[`\'"]', html)
        if m_asn:
            res["asndomain"] = m_asn.group(1).strip()

        m_org = re.search(r'window\.orgdomain\s*=\s*[`\'"](.*?)[`\'"]', html)
        if m_org:
            res["orgdomain"] = m_org.group(1).strip()

        m_use = re.search(r'class=[\'"]usecountbar[\'"][^>]*usecount=[\'"]([^\'"]+)[\'"]', html)
        if not m_use:
            m_use = re.search(r'class=[\'"]usecountbar[\'"][^>]*>(.*?)</div>', html, re.S)
        if m_use:
            use_txt = re.sub(r'<[^>]+>', '', m_use.group(1)).strip()
            if use_txt:
                res["usecount"] = use_txt

        res["status"] = "ok"
    except Exception as e:
        res["status"] = "failed"
        res["error"] = f"parse: {e}"
    return res

def _fetch_ipapi_is(ip: str, api_key: str, port: Optional[int]) -> Dict[str, Any]:
    """ipapi.is 深度安全/风控检测（Datacenter/VPN/Proxy/Tor/Abuser）。"""
    res: Dict[str, Any] = {
        "status": "pending",
        "is_datacenter": None,
        "is_vpn": None,
        "is_proxy": None,
        "is_tor": None,
        "is_abuser": None,
        "is_bogon": None,
        "abuser_score": None,
        "company": None,
        "company_type": None,
        "datacenter": None,
        "asn": None,
        "location": None,
        "has_key": bool(api_key),
        "error": None,
    }
    key_param = f"&key={api_key}" if api_key else ""
    endpoints = [
        f"https://sg.ipapi.is/?q={ip}{key_param}",
        f"https://us.ipapi.is/?q={ip}{key_param}",
        f"https://de.ipapi.is/?q={ip}{key_param}",
    ]
    raw = None
    err = None
    for url in endpoints:
        raw, err = _fetch_proxied(url, port, timeout=5.0) if port else (None, "no_port")
        if not raw:
            raw, err = _fetch_direct(url, timeout=5.0)
        if raw:
            break

    if not raw:
        res["status"] = "failed"
        res["error"] = err or "请求失败"
        return res

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            res["status"] = "failed"
            res["error"] = "非有效 JSON 格式"
            return res
        if data.get("error"):
            res["status"] = "failed"
            res["error"] = str(data.get("error"))
            return res

        res["status"] = "ok"
        res["is_bogon"] = bool(data.get("is_bogon")) if data.get("is_bogon") is not None else None
        res["is_datacenter"] = bool(data.get("is_datacenter")) if data.get("is_datacenter") is not None else None
        res["is_vpn"] = bool(data.get("is_vpn")) if data.get("is_vpn") is not None else None
        res["is_proxy"] = bool(data.get("is_proxy")) if data.get("is_proxy") is not None else None
        res["is_tor"] = bool(data.get("is_tor")) if data.get("is_tor") is not None else None
        res["is_abuser"] = bool(data.get("is_abuser")) if data.get("is_abuser") is not None else None
        res["abuser_score"] = data.get("abuser_score")

        comp = data.get("company")
        if isinstance(comp, dict):
            res["company"] = comp.get("name") or comp.get("domain") or None
            res["company_type"] = comp.get("type")
        elif isinstance(comp, str):
            res["company"] = comp

        dc = data.get("datacenter")
        if isinstance(dc, dict):
            res["datacenter"] = dc.get("datacenter") or dc.get("network_name")
            if res["is_datacenter"] is None:
                res["is_datacenter"] = True

        asn = data.get("asn")
        if isinstance(asn, dict):
            res["asn"] = asn.get("asn") or asn.get("descr")
        elif isinstance(asn, (str, int)):
            res["asn"] = str(asn)

        loc_parts = [data.get("city"), data.get("region"), data.get("country")]
        res["location"] = ", ".join([p for p in loc_parts if p]) or None
    except Exception as e:
        res["status"] = "failed"
        res["error"] = f"parse: {e}"

    return res


def judge_ip_quality(res: Dict[str, Any]) -> Dict[str, Any]:
    """根据多源交叉检测结果，提供日常代理适用性与住宅养号双维度综合裁决画像。"""
    ipap = res.get("ipapi") or {}
    scam = res.get("scamalytics") or {}
    ipqs = res.get("ipqualityscore") or {}
    p0 = res.get("ping0") or {}
    ipa = res.get("ippure") or {}
    ipis = res.get("ipapi_is") or {}

    # --- 1. 日常节点/科学代理模式 (Proxy Mode) ---
    proxy_reasons = []
    proxy_level = "PERFECT"
    proxy_cls = "ok"

    if ipis.get("is_abuser") is True:
        proxy_level = "REJECT"
        proxy_cls = "err"
        proxy_reasons.append("命中 ipapi.is 滥用者黑名单库 (Abuser)")
    if ipqs.get("recent_abuse") is True:
        proxy_level = "REJECT"
        proxy_cls = "err"
        proxy_reasons.append("命中 IPQualityScore 近期滥用黑名单 (Recent Abuse)")
    if ipqs.get("bot_status") is True:
        if proxy_level != "REJECT":
            proxy_level = "WARNING"
            proxy_cls = "warn"
        proxy_reasons.append("命中恶意 Bot 自动化流量行为库")

    scam_score = scam.get("score")
    ipqs_score = ipqs.get("fraud_score")
    p0_score = p0.get("score")

    high_scores = []
    if scam_score is not None and scam_score >= 60:
        high_scores.append(f"Scamalytics {scam_score}分")
    if ipqs_score is not None and ipqs_score >= 75:
        high_scores.append(f"IPQS {ipqs_score}分")
    if p0_score is not None and p0_score >= 60:
        high_scores.append(f"Ping0 {p0_score}分")

    if high_scores:
        proxy_level = "REJECT"
        proxy_cls = "err"
        proxy_reasons.append(f"多源欺诈分过高（{', '.join(high_scores)}），极易触发人机拦截/阻断")
    else:
        warn_scores = []
        if scam_score is not None and 30 <= scam_score < 60:
            warn_scores.append(f"Scamalytics {scam_score}分")
        if ipqs_score is not None and 40 <= ipqs_score < 75:
            warn_scores.append(f"IPQS {ipqs_score}分")
        if p0_score is not None and 35 <= p0_score < 60:
            warn_scores.append(f"Ping0 {p0_score}分")
        if warn_scores and proxy_level != "REJECT":
            proxy_level = "WARNING"
            proxy_cls = "warn"
            proxy_reasons.append(f"欺诈分偏高（{', '.join(warn_scores)}），敏感平台建议谨慎")

    p0_use = str(p0.get("usecount") or "")
    if any(k in p0_use for k in ("差", "危", "1000", "10000")):
        if proxy_level != "REJECT":
            proxy_level = "WARNING"
            proxy_cls = "warn"
        proxy_reasons.append(f"Ping0 标记全网独立共享人数偏高 ({p0_use})")

    if not proxy_reasons:
        proxy_summary = "极佳节点：信誉优良，无滥用与黑名单记录，欺诈分在安全绿区，推荐作为主力节点"
        proxy_reasons.append("多源交叉核验未发现滥用、恶意 Bot 或高危欺诈标记")
    elif proxy_level == "WARNING":
        proxy_summary = "可用但有瑕疵：存在轻微风控系数或邻居共享痕迹，用于常规网页浏览无碍，关键业务建议备用"
    else:
        proxy_summary = "高危警告：命中黑名单滥用记录或重度欺诈，极易吃验证码、封禁或直接被阻断"

    # --- 2. 住宅养号模式 (Residential / Clean Account Farming Mode) ---
    res_reasons = []
    res_level = "PERFECT"
    res_cls = "ok"

    dc_flags = []
    if ipap.get("hosting") is True:
        dc_flags.append("ip-api hosting")
    if ipis.get("is_datacenter") is True:
        dc_flags.append("ipapi.is datacenter")
    if scam.get("datacenter") is True:
        dc_flags.append("Scamalytics datacenter")
    if p0.get("type") == "IDC机房":
        dc_flags.append("Ping0 IDC机房")

    if dc_flags:
        res_level = "REJECT"
        res_cls = "err"
        res_reasons.append(f"已被识别为数据中心/机房IP（{', '.join(dc_flags)}）")

    proxy_flags = []
    if ipap.get("proxy") is True:
        proxy_flags.append("ip-api proxy")
    if ipis.get("is_proxy") is True or ipis.get("is_vpn") is True or ipis.get("is_tor") is True:
        proxy_flags.append("ipapi.is 代理/VPN")
    if scam.get("vpn") is True or scam.get("tor") is True:
        proxy_flags.append("Scamalytics 代理/VPN")
    if ipqs.get("proxy") is True or ipqs.get("vpn") is True or ipqs.get("tor") is True:
        proxy_flags.append("IPQS 代理/VPN")

    if proxy_flags:
        res_level = "REJECT"
        res_cls = "err"
        res_reasons.append(f"命中代理/VPN公共跳板（{', '.join(proxy_flags)}）")

    if ipis.get("is_abuser") is True or ipqs.get("recent_abuse") is True:
        res_level = "REJECT"
        res_cls = "err"
        res_reasons.append("命中黑名单滥用记录 (Abuser)")

    if scam_score is not None and scam_score >= 20:
        if res_level != "REJECT":
            res_level = "WARNING"
            res_cls = "warn"
        res_reasons.append(f"Scamalytics 欺诈分 {scam_score} 超出养号安全区 (<20)")

    if p0.get("native") is False:
        if res_level != "REJECT":
            res_level = "WARNING"
            res_cls = "warn"
        res_reasons.append("属于广播 IP 非原生本地住宅分配")

    if not res_reasons:
        res_summary = "完美纯净家宽：真实原生住宅血统，零机房与代理标记，低欺诈分，极度适宜海外养号与深度风控穿透"
        res_reasons.append("未检出任何 Hosting/机房/代理跳板标记，欺诈分全绿")
    elif res_level == "WARNING":
        res_summary = "疑似商宽或有瑕疵住宅：未检出纯机房，但欺诈分偏高或为广播IP，做养号主力存在风控风险"
    else:
        res_summary = "不适宜住宅养号：已被识别为机房商宽、代理跳板或黑名单，用于海外养号极易触发风控封号"

    return {
        "proxy_mode": {
            "level": proxy_level,
            "badge": "优秀可用" if proxy_level == "PERFECT" else ("风险预警" if proxy_level == "WARNING" else "不建议使用"),
            "cls": proxy_cls,
            "summary": proxy_summary,
            "reasons": proxy_reasons,
        },
        "residential_mode": {
            "level": res_level,
            "badge": "纯净家宽" if res_level == "PERFECT" else ("存疑/商宽" if res_level == "WARNING" else "非住宅(拒用)"),
            "cls": res_cls,
            "summary": res_summary,
            "reasons": res_reasons,
        }
    }


def probe_ip_info(target_ip: str) -> Dict[str, Any]:
    """查询任意 IP 的综合画像（IPPure 官方 API + 多源交叉校验 + 反向 DNS + Scamalytics + IPQualityScore + Ping0）。"""
    ip = (target_ip or "").strip()
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return {"ok": False, "error": "IP 格式无效（支持 IPv4 / IPv6）"}
    ip = str(parsed)

    _, _, port = _get_verge_paths()
    ipqs_key, ping0_key, ipapi_is_key = _get_api_keys()

    result: Dict[str, Any] = {
        "ok": True,
        "ip": ip,
        "ip_version": f"IPv{parsed.version}",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ippure": {"basic": None, "risk_score": None, "botclass": None, "error": None},
        "ipapi": {"status": "pending", "error": None},
        "ipinfo": {"status": "pending", "error": None},
        "ipsb": {"status": "pending", "error": None},
        "ipwho": {"status": "pending", "error": None},
        "scamalytics": {"status": "pending", "score": None, "risk": None, "error": None},
        "ipqualityscore": {"status": "pending", "fraud_score": None, "error": None},
        "ping0": {"status": "pending", "score": None, "error": None},
        "ipapi_is": {"status": "pending", "is_datacenter": None, "is_abuser": None, "error": None},
        "rdns": {"status": "pending", "hostname": None, "error": None},
    }

    ipapi_url = f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,query,as,asname,isp,org,mobile,proxy,hosting"
    jobs = {
        "ipapi": lambda: _fetch_proxied(ipapi_url, port),
        "ipinfo": lambda: _fetch_proxied(f"https://ipinfo.io/{ip}/json", port),
        "ipsb": lambda: _fetch_proxied(f"https://api.ip.sb/geoip/{ip}", port),
        "ipwho": lambda: _fetch_proxied(IPWHO_BASE + ip, port),
    }
    fetched: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(jobs) + 6) as pool:
        futures = {pool.submit(_retry_once, job): key for key, job in jobs.items()}
        futures[pool.submit(_ippure_reports, [ip], port)] = "ippure"
        futures[pool.submit(_reverse_dns, ip)] = "rdns"
        futures[pool.submit(_fetch_scamalytics, ip, port)] = "scamalytics"
        futures[pool.submit(_fetch_ipqs, ip, ipqs_key, port)] = "ipqualityscore"
        futures[pool.submit(_fetch_ping0, ip, ping0_key, port)] = "ping0"
        futures[pool.submit(_fetch_ipapi_is, ip, ipapi_is_key, port)] = "ipapi_is"
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                fetched[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                fetched[key] = None if key == "ippure" else (None, type(e).__name__)

    # IPPure 报告
    rep = fetched.get("ippure")
    if isinstance(rep, dict):
        result["ippure"] = rep.get(ip) or {"basic": None, "risk_score": None, "botclass": None, "error": "未返回数据"}
    else:
        result["ippure"] = {"basic": None, "risk_score": None, "botclass": None, "error": "IPPure 接口不可达"}

    # ip-api（复用面板解析器）
    raw, err = fetched.get("ipapi") or (None, None)
    if raw:
        try:
            d, perr = _parse_ipapi(raw)
            if d:
                result["ipapi"].update(d)
                result["ipapi"]["status"] = "ok"
            else:
                result["ipapi"].update({"status": "failed", "error": perr})
        except Exception as e:  # noqa: BLE001
            result["ipapi"].update({"status": "failed", "error": f"parse: {e}"})
    else:
        result["ipapi"].update({"status": "failed", "error": err})

    pair = fetched.get("ipinfo") or (None, None)
    _apply_json_source(result["ipinfo"], pair[0], pair[1], _map_ipinfo_block)
    pair = fetched.get("ipsb") or (None, None)
    _apply_json_source(result["ipsb"], pair[0], pair[1], _map_ipsb_block)
    pair = fetched.get("ipwho") or (None, None)
    _apply_json_source(result["ipwho"], pair[0], pair[1], _map_ipwho_block)

    # Scamalytics, IPQualityScore, Ping0
    scam_res = fetched.get("scamalytics")
    if isinstance(scam_res, dict):
        result["scamalytics"] = scam_res
    else:
        result["scamalytics"] = {"status": "failed", "error": "查询未返回"}

    ipqs_res = fetched.get("ipqualityscore")
    if isinstance(ipqs_res, dict):
        result["ipqualityscore"] = ipqs_res
    else:
        result["ipqualityscore"] = {"status": "failed", "error": "查询未返回"}

    ping0_res = fetched.get("ping0")
    if isinstance(ping0_res, dict):
        result["ping0"] = ping0_res
    else:
        result["ping0"] = {"status": "failed", "error": "查询未返回"}

    ipis_res = fetched.get("ipapi_is")
    if isinstance(ipis_res, dict):
        result["ipapi_is"] = ipis_res
    else:
        result["ipapi_is"] = {"status": "failed", "error": "查询未返回"}

    rdns = fetched.get("rdns")
    if isinstance(rdns, dict):
        result["rdns"] = rdns

    result["quality_judge"] = judge_ip_quality(result)

    return result


def probe_neighbors() -> Dict[str, Any]:
    """IPPure 网上邻居（同 IP / /24 网段留言板，当前出口视角） + Ping0 全网独立用户共享人数。

    数据源 = ippure /api/message/list + Ping0 当前出口 usecount。
    """
    _, _, port = _get_verge_paths()
    _, ping0_key, *_ = _get_api_keys()

    # 获取当前出口 IP 及其 Ping0 共享人数
    cur_ip = None
    ping0_share = None
    with _CACHE_LOCK:
        if _CACHE.get("current_ip") and _CACHE["current_ip"].get("proxy_egress"):
            pe = _CACHE["current_ip"]["proxy_egress"]
            cur_ip = pe.get("ip")
            if isinstance(pe.get("ping0"), dict):
                ping0_share = pe["ping0"].get("usecount")
    if not cur_ip:
        c = probe_current_ip()
        pe = c.get("proxy_egress") or {}
        cur_ip = pe.get("ip")
        if isinstance(pe.get("ping0"), dict):
            ping0_share = pe["ping0"].get("usecount")

    if not ping0_share and cur_ip:
        p0 = _fetch_ping0(cur_ip, ping0_key, port)
        if p0.get("status") == "ok":
            ping0_share = p0.get("usecount")

    last_err: Optional[str] = None
    for route in ([None, port] if port else [None]):
        client = _IPPureAPIClient(proxy_port=route)
        try:
            client.bootstrap()
            data = client.get_json("/api/message/list")
            if not data.get("ok"):
                raise OSError(str(data.get("message") or data.get("error") or "接口返回异常"))
            raw = data.get("data")
            items = raw if isinstance(raw, list) else []
            users: List[str] = []
            msgs: List[Dict[str, Any]] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                user = str(it.get("username") or it.get("user") or it.get("name") or "").strip()
                text = str(it.get("content") or it.get("message") or it.get("text") or "").strip()
                ts = str(it.get("createdAt") or it.get("created_at") or it.get("time") or "").strip()
                if user and user not in users:
                    users.append(user)
                msgs.append({"user": user or "匿名", "text": text[:120], "time": ts[:32]})
            return {"ok": True, "count_users": len(users), "count_messages": len(msgs),
                    "messages": msgs[:5], "ping0_usecount": ping0_share, "error": None}
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        finally:
            client.close()
    return {"ok": False, "count_users": 0, "count_messages": 0, "messages": [], "ping0_usecount": ping0_share, "error": last_err or "查询失败"}


def _find_chrome() -> Optional[str]:
    for path in CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _parse_cf_page(dom: str) -> Dict[str, Any]:
    """从 ippure 云防检测页 DOM 抓取：风险指数 / CF 定位 / 用途检测标记"""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", dom, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    m = re.search(r"风险指数[:：]?\s*(\d+)\s*/\s*100", text)
    if not m:
        return {"ok": False, "error": "页面未返回风险指数（可能未加载完成）"}
    score = int(m.group(1))

    loc = ""
    lm = re.search(r"地理位置定位\s+(.+?)\s*风险用途检测", text)
    if lm:
        loc = lm.group(1).strip()

    flags: Dict[str, Any] = {}
    for name, key in (("公开代理", "open_proxy"), ("住宅代理", "residential"), ("企业代理", "corporate")):
        idx = text.find(name)
        seg = text[idx:idx + 40] if idx >= 0 else ""
        if "未检测到" in seg:
            flags[key] = False
        elif "检测到" in seg:
            flags[key] = True
        else:
            flags[key] = None

    return {"ok": True, "score": score, "loc": loc, "flags": flags}


def probe_cf_score(side: str = "direct", force: bool = False) -> Dict[str, Any]:
    """Cloudflare 云防风险指数（ippure 云防检测页的「风险指数 /100」，源自 Cloudflare Bot Management）。

    该数据必须浏览器环境模拟（Turnstile/Bot Management），用本机 Chrome 无头运行抓取；
    side=proxy 时让 Chrome 走本机混合端口（对齐「代理出站」语义），side=direct 走直连。
    结果内存缓存 30 分钟；同一侧并发检测会直接拒绝。
    """
    key = side if side in _CF_LOCKS else "direct"
    now = time.time()
    with _CACHE_LOCK:
        cached = _CF_CACHE.get(key)
    if cached and not force and (now - cached["ts"] < CF_SCORE_TTL):
        data = dict(cached["data"])
        data["cached"] = True
        return data
    if not force:
        return {"ok": False, "side": key, "not_cached": True, "error": "尚未检测（点「运行检测」触发）"}

    lock = _CF_LOCKS[key]
    if not lock.acquire(blocking=False):
        return {"ok": False, "side": key, "error": "已有检测在进行中，请稍候重试"}

    profile = None
    t0 = time.time()
    try:
        chrome = _find_chrome()
        if not chrome:
            return {"ok": False, "side": key, "error": "未找到本机 Chrome（Program Files）"}
        _, _, port = _get_verge_paths()
        profile = tempfile.mkdtemp(prefix="xy-cf-")
        args = [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-extensions",
            "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
            f"--user-data-dir={profile}",
        ]
        if key == "proxy" and port:
            args.append(f"--proxy-server=http://127.0.0.1:{port}")
        args += ["--virtual-time-budget=70000", "--dump-dom", IPPURE_CF_PAGE_URL]
        proc = subprocess.run(
            args, capture_output=True, timeout=CF_RUN_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        dom = (proc.stdout or b"").decode("utf-8", errors="replace")
        parsed = _parse_cf_page(dom)
        if not parsed.get("ok"):
            return {"ok": False, "side": key, "error": parsed.get("error") or "页面解析失败",
                    "elapsed": round(time.time() - t0, 1)}
        parsed.update({
            "ok": True, "side": key, "elapsed": round(time.time() - t0, 1),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cached": False,
        })
        with _CACHE_LOCK:
            _CF_CACHE[key] = {"ts": time.time(), "data": parsed}
        return parsed
    except subprocess.TimeoutExpired:
        return {"ok": False, "side": key, "error": f"检测超时（>{CF_RUN_TIMEOUT}s）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "side": key, "error": f"{type(e).__name__}: {e}"}
    finally:
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
        lock.release()


# ---------- 核心功能 1：当前 IP 状态检测 ----------

def probe_current_ip(force: bool = False) -> Dict[str, Any]:
    """探测当前代理出口与本地直连出口的多源画像。

    代理侧：IPPure（主源）+ Ping0 + Cloudflare 视角 / ip-api / ipinfo / IP.SB；
    直连侧：ip-api + Cloudflare 视角 / ipinfo / IP.SB。全部外部探针并发拉取。
    """
    global _CACHE
    now = time.time()
    with _CACHE_LOCK:
        if not force and _CACHE["current_ip"] and (now - _CACHE["current_ip_ts"] < CACHE_TTL):
            return _CACHE["current_ip"]

    _, _, port = _get_verge_paths()

    result: Dict[str, Any] = {
        "ok": True,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mixed_port": port,
        "proxy_egress": {
            "status": "pending",
            "ip": None,
            "country": None,
            "city": None,
            "asn": None,
            "org": None,
            "fraud_score": None,
            "is_residential": None,
            "is_broadcast": None,
            "raw_ippure": None,
            "ping0": None,
            "scamalytics": None,
            "ipqualityscore": None,
            "error": None,
            "ippure_api": {"basic": None, "risk_score": None, "botclass": None, "error": None},
            **_new_ext_blocks(),
        },
        "direct_egress": {
            "status": "pending",
            "ip": None,
            "country": None,
            "region": None,
            "city": None,
            "isp": None,
            "asn": None,
            "ping0": None,
            "scamalytics": None,
            "ipqualityscore": None,
            "error": None,
            "ippure_api": {"basic": None, "risk_score": None, "botclass": None, "error": None},
            **_new_ext_blocks(),
        },
        "active_proxies": {}
    }

    # 0. 并发拉取全部外部探针（原串行约 7 秒，并发后 ≈ 最慢单路耗时）
    jobs = {
        "ippure_proxy": lambda: _fetch_proxied(IPPURE_URL, port),
        "ping0_proxy": lambda: _fetch_proxied(PING0_GEO_URL, port),
        "cf_proxy": lambda: _fetch_cf_trace(port),
        "ipapi_proxy": lambda: _fetch_proxied(IPAPI_FULL_URL, port),
        "ipinfo_proxy": lambda: _fetch_proxied(IPINFO_URL, port),
        "ipsb_proxy": lambda: _fetch_proxied(IPSB_GEO_URL, port),
        "ipwho_proxy": lambda: _fetch_proxied(IPWHO_BASE, port),
        "cf_direct": lambda: _fetch_cf_trace(None),
        "ipapi_direct": lambda: _fetch_direct(IPAPI_FULL_URL),
        "ipinfo_direct": lambda: _fetch_direct(IPINFO_URL),
        "ipsb_direct": lambda: _fetch_direct(IPSB_GEO_URL),
        "ipwho_direct": lambda: _fetch_direct(IPWHO_BASE),
    }
    fetched: Dict[str, Tuple[Optional[bytes], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(_retry_once, job): key for key, job in jobs.items()}
        for fut in as_completed(futures):
            fetched[futures[fut]] = fut.result()

    # 1. 代理出口：IPPure（主源）
    ippure_bytes, ippure_err = fetched["ippure_proxy"]
    if ippure_bytes:
        try:
            p_data = json.loads(ippure_bytes.decode("utf-8"))
            result["proxy_egress"]["status"] = "ok"
            result["proxy_egress"]["ip"] = p_data.get("ip")
            result["proxy_egress"]["country"] = p_data.get("country")
            result["proxy_egress"]["city"] = p_data.get("city")
            result["proxy_egress"]["asn"] = p_data.get("asn")
            result["proxy_egress"]["org"] = p_data.get("asOrganization")
            result["proxy_egress"]["fraud_score"] = p_data.get("fraudScore")
            result["proxy_egress"]["is_residential"] = p_data.get("isResidential")
            result["proxy_egress"]["is_broadcast"] = p_data.get("isBroadcast")
            result["proxy_egress"]["raw_ippure"] = p_data
        except Exception as e:
            result["proxy_egress"]["error"] = f"解析 IPPure 失败: {e}"
    else:
        result["proxy_egress"]["error"] = f"请求 IPPure 失败: {ippure_err}"

    # 2. 代理出口：Ping0 /geo（兜底源）
    ping0_bytes, ping0_err = fetched["ping0_proxy"]
    if ping0_bytes:
        try:
            lines = [l.strip() for l in ping0_bytes.decode("utf-8", errors="replace").splitlines() if l.strip()]
            if len(lines) >= 3:
                result["proxy_egress"]["ping0"] = {
                    "ip": lines[0],
                    "location": lines[1] if len(lines) > 1 else "",
                    "asn": lines[2] if len(lines) > 2 else "",
                    "isp": lines[3] if len(lines) > 3 else ""
                }
                # 若 IPPure 失败，用 ping0 兜底 IP 和 ASN
                if not result["proxy_egress"]["ip"]:
                    result["proxy_egress"]["ip"] = lines[0]
                    result["proxy_egress"]["country"] = lines[1]
                    result["proxy_egress"]["org"] = lines[3] if len(lines) > 3 else lines[2]
                    result["proxy_egress"]["status"] = "ok_ping0_fallback"
        except Exception:
            pass

    # 3. 代理出口多源：Cloudflare 视角 / ip-api 风控 / ipinfo / IP.SB / ipwho.is
    _apply_ipapi_block(result["proxy_egress"], *fetched["ipapi_proxy"])
    _apply_ext_sources(result["proxy_egress"], fetched["cf_proxy"], fetched["ipinfo_proxy"],
                       fetched["ipsb_proxy"], fetched["ipwho_proxy"])

    # 4. 本地直连出口：ip-api（isp/asn 旧字段复用同一次解析）
    d_data = _apply_ipapi_block(result["direct_egress"], *fetched["ipapi_direct"])
    if d_data:
        result["direct_egress"]["status"] = "ok"
        result["direct_egress"]["ip"] = d_data["ip"]
        result["direct_egress"]["country"] = d_data["country"]
        result["direct_egress"]["region"] = d_data["region"]
        result["direct_egress"]["city"] = d_data["city"]
        result["direct_egress"]["isp"] = d_data["isp"]
        result["direct_egress"]["asn"] = d_data["asn"]
    else:
        direct_raw, direct_err = fetched["ipapi_direct"]
        if direct_raw:
            result["direct_egress"]["error"] = f"解析直连 IP 失败: {result['direct_egress']['ipapi'].get('error') or 'invalid_json'}"
        else:
            result["direct_egress"]["error"] = f"获取直连 IP 失败: {direct_err}"

    # 5. 本地直连出口多源：Cloudflare 视角 / ipinfo / IP.SB / ipwho.is（ip-api 已在步骤 4 处理）
    _apply_ext_sources(result["direct_egress"], fetched["cf_direct"], fetched["ipinfo_direct"],
                       fetched["ipsb_direct"], fetched["ipwho_direct"])

    # 6. IPPure 官方报告（ip-basic / ip-risk / botclass，一次复用连接查两侧出口）
    side_ips = [result[k].get("ip") for k in ("proxy_egress", "direct_egress")]
    ippure_map = _ippure_reports([ip for ip in side_ips if ip], port)
    for side_key in ("proxy_egress", "direct_egress"):
        ip_val = result[side_key].get("ip")
        if ip_val and ippure_map.get(ip_val):
            result[side_key]["ippure_api"] = ippure_map[ip_val]
        else:
            result[side_key]["ippure_api"] = {
                "basic": None, "risk_score": None, "botclass": None,
                "error": "无出口 IP" if not ip_val else "IPPure 查询未返回",
            }

    # 6.5 深度安全画像：Scamalytics + IPQualityScore + Ping0 + ipapi.is（含共享人数与滥用库）
    ipqs_key, ping0_key, ipapi_is_key = _get_api_keys()
    deep_jobs = {}
    p_ip = result["proxy_egress"].get("ip")
    if p_ip:
        deep_jobs["scam_proxy"] = lambda: _fetch_scamalytics(p_ip, port)
        deep_jobs["ipqs_proxy"] = lambda: _fetch_ipqs(p_ip, ipqs_key, port)
        deep_jobs["ping0_proxy"] = lambda: _fetch_ping0(p_ip, ping0_key, port)
        deep_jobs["ipapi_is_proxy"] = lambda: _fetch_ipapi_is(p_ip, ipapi_is_key, port)
    d_ip = result["direct_egress"].get("ip")
    if d_ip:
        deep_jobs["scam_direct"] = lambda: _fetch_scamalytics(d_ip, None)
        deep_jobs["ipqs_direct"] = lambda: _fetch_ipqs(d_ip, ipqs_key, None)
        deep_jobs["ping0_direct"] = lambda: _fetch_ping0(d_ip, ping0_key, None)
        deep_jobs["ipapi_is_direct"] = lambda: _fetch_ipapi_is(d_ip, ipapi_is_key, None)

    if deep_jobs:
        with ThreadPoolExecutor(max_workers=min(len(deep_jobs), 6)) as pool:
            futs = {pool.submit(fn): k for k, fn in deep_jobs.items()}
            for f in as_completed(futs):
                k = futs[f]
                try:
                    res_obj = f.result()
                except Exception as ex:
                    res_obj = {"status": "failed", "error": str(ex)}
                if k == "scam_proxy":
                    result["proxy_egress"]["scamalytics"] = res_obj
                elif k == "ipqs_proxy":
                    result["proxy_egress"]["ipqualityscore"] = res_obj
                elif k == "ping0_proxy":
                    old_p0 = result["proxy_egress"].get("ping0") or {}
                    if isinstance(old_p0, dict):
                        for ok, ov in old_p0.items():
                            if ov and not res_obj.get(ok):
                                res_obj[ok] = ov
                    result["proxy_egress"]["ping0"] = res_obj
                elif k == "ipapi_is_proxy":
                    result["proxy_egress"]["ipapi_is"] = res_obj
                elif k == "scam_direct":
                    result["direct_egress"]["scamalytics"] = res_obj
                elif k == "ipqs_direct":
                    result["direct_egress"]["ipqualityscore"] = res_obj
                elif k == "ping0_direct":
                    result["direct_egress"]["ping0"] = res_obj
                elif k == "ipapi_is_direct":
                    result["direct_egress"]["ipapi_is"] = res_obj

    # 7. 读取当前 Mihomo 活跃策略组状态
    st, proxies_data = mihomo_api_call("/proxies")
    if st == 200 and isinstance(proxies_data, dict):
        proxies_map = proxies_data.get("proxies", {})
        active_summary = {}
        # 提取关键组（2026-10-13 对齐当前配置结构；旧组名已移除，缺失的组自动跳过）
        for gname in ["GLOBAL", "🚀 节点选择", "🛡️ 日常组", "🎯 敏感组",
                      "🟢 双绿IP组", "🌍 小众地区组", "⚡ 住宅中转", "🔒 DNS固定通道",
                      "⚡ 自动选择", "🇸🇬 新加坡组", "🇺🇸 美国组", "🇯🇵 日本组"]:
            if gname in proxies_map:
                active_summary[gname] = {
                    "type": proxies_map[gname].get("type"),
                    "now": proxies_map[gname].get("now"),
                }
        result["active_proxies"] = active_summary

    with _CACHE_LOCK:
        _CACHE["current_ip"] = result
        _CACHE["current_ip_ts"] = now

    return result


# ---------- 核心功能 2：目标网站链路溯源 ----------

def trace_target_website(target_domain: str) -> Dict[str, Any]:
    """溯源目标域名的 DNS 解析、命中的 Clash 分流规则、出站策略组与出口画像"""
    domain = target_domain.strip().lower()
    # 过滤 url scheme
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0].split(":")[0]
    
    if not domain:
        return {"ok": False, "error": "域名不能为空"}
        
    trace_res: Dict[str, Any] = {
        "ok": True,
        "target": domain,
        "dns_resolution": {
            "status": "pending",
            "answers": [],
            "raw": None
        },
        "rule_match": {
            "matched": False,
            "type": None,
            "payload": None,
            "target_proxy": None,
            "rule_index": -1
        },
        "outbound_chain": [],
        "estimated_egress": {
            "type": "UNKNOWN",
            "desc": ""
        }
    }

    # 1. 查询 Mihomo DNS
    st, dns_res = mihomo_api_call(f"/dns/query?name={domain}&type=A")
    if st == 200 and isinstance(dns_res, dict):
        trace_res["dns_resolution"]["status"] = "ok"
        answers = []
        for ans in dns_res.get("Answer", []):
            if ans.get("data"):
                answers.append({
                    "ip": ans.get("data"),
                    "ttl": ans.get("TTL"),
                    "type": ans.get("type")
                })
        trace_res["dns_resolution"]["answers"] = answers
        trace_res["dns_resolution"]["raw"] = dns_res
    else:
        trace_res["dns_resolution"]["status"] = "failed"

    # 2. 匹配规则
    st, rules_res = mihomo_api_call("/rules")
    matched_rule = None
    if st == 200 and isinstance(rules_res, dict):
        rules = rules_res.get("rules", [])
        for r in rules:
            rtype = r.get("type", "")
            payload = (r.get("payload") or "").lower()
            
            if rtype == "Domain" and domain == payload:
                matched_rule = r; break
            elif rtype == "DomainSuffix" and (domain == payload or domain.endswith("." + payload)):
                matched_rule = r; break
            elif rtype == "DomainKeyword" and payload in domain:
                matched_rule = r; break
            elif rtype == "Match":
                matched_rule = r; break
                
    if matched_rule:
        target_group = matched_rule.get("proxy", "")
        trace_res["rule_match"] = {
            "matched": True,
            "type": matched_rule.get("type"),
            "payload": matched_rule.get("payload"),
            "target_proxy": target_group,
            "rule_index": matched_rule.get("index", -1)
        }
        
        # 3. 追踪出站链 (寻找当前生效节点)
        chain = [target_group]
        st_p, proxies_res = mihomo_api_call("/proxies")
        if st_p == 200 and isinstance(proxies_res, dict):
            proxies_map = proxies_res.get("proxies", {})
            curr = target_group
            visited = set()
            while curr in proxies_map and curr not in visited:
                visited.add(curr)
                node_info = proxies_map[curr]
                now_node = node_info.get("now")
                if now_node and now_node != curr:
                    chain.append(now_node)
                    curr = now_node
                else:
                    break
        trace_res["outbound_chain"] = chain

        # 4. 判断出口性质
        final_node = chain[-1] if chain else target_group
        if final_node.upper() == "DIRECT":
            trace_res["estimated_egress"] = {
                "type": "DIRECT",
                "desc": "直连模式：该网站流量直连本机真实宽带，不经过任何代理。"
            }
        elif final_node.upper() == "REJECT":
            trace_res["estimated_egress"] = {
                "type": "REJECT",
                "desc": "拦截模式：该网站流量被 Clash 规则主动阻断 (REJECT)。"
            }
        elif "住宅" in final_node or "住宅" in target_group:
            trace_res["estimated_egress"] = {
                "type": "RESIDENTIAL",
                "desc": f"住宅策略路由：走 [{target_group}] 专用通道出口，隔离纯净度高。"
            }
        else:
            trace_res["estimated_egress"] = {
                "type": "PROXY",
                "desc": f"常规代理出站：流量流向 [{final_node}] 节点出口。"
            }
            
    return trace_res


# ---------- 核心功能 3：DNS 诊断与泄漏测试 ----------

def probe_dns_diagnostics() -> Dict[str, Any]:
    """检测 DoH 上游、Fake-IP 与 DNS 泄漏"""
    global _CACHE
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE["dns_status"] and (now - _CACHE["dns_status_ts"] < CACHE_TTL):
            return _CACHE["dns_status"]

    _, _, port = _get_verge_paths()
    
    diag: Dict[str, Any] = {
        "ok": True,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "leak_detected": False,
        "upstream_resolver": {
            "status": "pending",
            "ip": None,
            "geo": None,
            "asn": None
        },
        "fake_ip_mode": {
            "enabled": True,
            "probe_domain": "clash-probe-test.internal",
            "fake_ip": None,
            "is_fake_ip_range": False
        },
        "doh_status": "active"
    }

    # 1. 探测 DNS 出口服务器 (EDNS)
    edns_bytes, edns_err = _fetch_proxied(EDNS_LEAK_URL, port)
    if edns_bytes:
        try:
            edns_data = json.loads(edns_bytes.decode("utf-8"))
            dns_info = edns_data.get("dns", {})
            r_ip = dns_info.get("ip")
            r_geo = dns_info.get("geo", "")
            
            diag["upstream_resolver"]["status"] = "ok"
            diag["upstream_resolver"]["ip"] = r_ip
            diag["upstream_resolver"]["geo"] = r_geo
            
            # 判断泄漏：若出国代理时，递归 DNS 返回了国内运营商 (China / Telecom / Unicom / Mobile / 114)
            lower_geo = r_geo.lower()
            if any(cn_tag in lower_geo for cn_tag in ["china", "telecom", "unicom", "mobile", "114", "alidns", "tencent"]):
                diag["leak_detected"] = True
                diag["leak_reason"] = f"检测到国内上游 DNS 服务器 ({r_geo})，可能存在 DNS 泄露！"
            else:
                diag["leak_detected"] = False
                diag["leak_reason"] = f"上游 DNS 服务器归属境外安全节点 ({r_geo})，未发生泄漏。"
        except Exception as e:
            diag["upstream_resolver"]["status"] = "parse_error"
            diag["upstream_resolver"]["error"] = str(e)
    else:
        diag["upstream_resolver"]["status"] = "network_error"
        diag["upstream_resolver"]["error"] = edns_err

    # 2. 探测 Fake-IP
    st, q_res = mihomo_api_call(f"/dns/query?name={diag['fake_ip_mode']['probe_domain']}&type=A")
    if st == 200 and isinstance(q_res, dict):
        answers = q_res.get("Answer", [])
        if answers and answers[0].get("data"):
            ip_str = answers[0]["data"]
            diag["fake_ip_mode"]["fake_ip"] = ip_str
            # Fake-ip 段一般在 198.18.0.0/16
            if ip_str.startswith("198.18.") or ip_str.startswith("198.19."):
                diag["fake_ip_mode"]["is_fake_ip_range"] = True

    with _CACHE_LOCK:
        _CACHE["dns_status"] = diag
        _CACHE["dns_status_ts"] = now

    return diag
