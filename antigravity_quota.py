# -*- coding: utf-8 -*-
"""验证：直接请求 Antigravity 语言服务器的官方配额接口。

方法来自 https://github.com/NaHSIT/Antigravity-Usage （不装 CA、不抓包）：
  1) 从 language_server.exe 进程命令行取 --csrf_token
  2) 找到它的监听端口
  3) POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary
     Header: X-Codeium-Csrf-Token / Connect-Protocol-Version: 1
"""
import ctypes
import json
import re
import ssl
import subprocess
import urllib.request

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def ls_info():
    """返回 (pid, port, csrf_token)。"""
    # creationflags=CREATE_NO_WINDOW：被 windowed（无控制台）进程调用时防止闪现黑窗
    out = subprocess.run(
        ["wmic", "process", "where", "Name='language_server.exe'",
         "get", "ProcessId,CommandLine", "/format:csv"],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW).stdout
    pid = token = None
    for line in out.splitlines():
        if "language_server.exe" not in line:
            continue
        m = re.search(r"(\d+)\s*$", line.strip())
        if m:
            pid = m.group(1)
        m2 = re.search(r"--csrf_token[= ]+([0-9a-fA-F-]{8,})", line)
        if m2:
            token = m2.group(1)
    if not pid:
        return None, None, None
    # 找该 pid 的监听端口
    nets = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                          timeout=30, errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW).stdout
    ports = []
    for line in nets.splitlines():
        if "LISTENING" in line and f" {pid}" in line:
            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            if m:
                ports.append(int(m.group(1)))
    return int(pid), sorted(set(ports)), token


def quota(port, token, timeout=20):
    url = (f"https://127.0.0.1:{port}/exa.language_server_pb."
           f"LanguageServerService/RetrieveUserQuotaSummary")
    body = json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "X-Codeium-Csrf-Token": token or "",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # 本地 LS 自签证书
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),      # 直连，别走系统代理
        urllib.request.HTTPSHandler(context=ctx))
    with op.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


if __name__ == "__main__":
    pid, ports, token = ls_info()
    print("LS pid:", pid, "| 监听端口:", ports, "| token:", (token or "")[:8] + "...")
    ok = False
    for p in ports or []:
        try:
            st, data = quota(p, token)
            print(f"\n=== 端口 {p} 成功 (HTTP {st}) ===")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1800])
            ok = True
            break
        except Exception as e:
            print(f"  端口 {p} 失败: {type(e).__name__} {str(e)[:120]}")
    if not ok:
        print("\n本地接口都不通（稍后可试远程 cloudcode-pa 兜底）")
