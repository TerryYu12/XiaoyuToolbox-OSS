# -*- coding: utf-8 -*-
"""Qwen3.5 本地翻译后端：常驻 llama-server + OpenAI 兼容 API。

关键点（实测）：
  - 必须 --jinja + --reasoning-format none，否则 Qwen3.5 输出思维链，
    70-80% token 浪费在自我推演上，有效产出≈0。
  - chat_template_kwargs.enable_thinking=False 是 API 侧等价开关。
"""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path

HERMES = os.environ.get("LOCALAPPDATA", r"C:\Users\11\AppData\Local")
# 注意：runtimes/ 和 models/ 都在 hermes/ 目录下，不是直接在 Local 下
RT = Path(HERMES) / "hermes" / "runtimes" / "llamacpp" / "b10679" / "cuda"
# models 是 junction -> F:\AI Flies\hermes work\hermes-models，必须走 hermes 这一层
MODELS = Path(HERMES) / "hermes" / "models"
# junction 失效时的兜底真身
MODELS_FALLBACK = Path(r"F:\AI Flies\hermes work\hermes-models")

PORT = 8899
HOST = "127.0.0.1"

# 翻译方向 -> 系统提示。temperature 低保证稳定，不花哨。
SYS_ZH = (
    "You are a professional literary translator. "
    "Translate the user text into natural, fluent Chinese. "
    "Output ONLY the translation. No explanations, no notes, no source echo."
)
SYS_EN = (
    "You are a professional translator. "
    "Translate the user text into natural English. "
    "Output ONLY the translation. No explanations, no notes, no source echo."
)


def server_path():
    return RT / "llama-server.exe"


def model_path(name="Qwen3.5-4B-Q4_K_M.gguf"):
    p = MODELS / name
    if p.exists():
        return p
    fb = MODELS_FALLBACK / name
    if fb.exists():
        return fb
    raise FileNotFoundError(f"模型不存在: {p}（也试了兜底 {fb}）")


def port_busy(port=PORT):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def start_server(model="Qwen3.5-4B-Q4_K_M.gguf", ngl=99, threads=6, ctx=65536):
    """启动 llama-server（隐藏控制台），返回进程对象。"""
    if port_busy():
        raise RuntimeError(f"端口 {PORT} 已被占用")
    exe = str(server_path())
    mp = str(model_path(model))
    # CREATE_NO_WINDOW: venv pythonw 是跳板会 re-exec，DETACHED 会飘可见黑窗
    flags = 0x08000000 | 0x00000200 | 0x01000000  # NO_WINDOW | NEW_PROC_GROUP | BREAKAWAY
    cmd = [
        exe, "-m", mp, "--jinja", "--alias", "Qwen3.5-4B-Q4_K_M",
        # 2026-09-19: 关闭思考链。实测（b10679 + Qwen3.5-4B，150 tok 请求）：
        #   --chat-template-kwargs '{"enable_thinking":false}' → 无效，仍烧满思考
        #   --reasoning-budget 0                               → 无效
        #   -rea off                                           → 生效（150tok→43tok，3.3s→1.1s）
        # 且 --reasoning-format none 与 -rea off 叠加会残留空 <think></think> 标签在正文，
        # 改用 auto：think 若出现则归入 reasoning_content，正文保持干净。
        "-rea", "off",
        "--reasoning-format", "auto",
        "-ngl", str(ngl), "-t", str(threads), "-fa", "1",
        "-c", str(ctx), "--port", str(PORT), "--host", HOST, "--no-webui",
    ]
    return subprocess.Popen(cmd, creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(timeout=180):
    """轮询 /health 直到就绪。"""
    deadline = time.time() + timeout
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        try:
            r = op.open(f"http://{HOST}:{PORT}/health", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.7)
    return False


def strip_think(text):
    """剥掉 <think>...</think> 块。llama-server 的 --reasoning-format none
    在 Qwen3.5 上仍会残留空 think 标签，必须代码层兜底。"""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


class ThinkFilter:
    """流式 think 过滤器：<think>...</think> 标签可能被 chunk 切断，
    用状态机 + 前缀保护，保证不输出半截标签。"""

    def __init__(self):
        self.buf = ""
        self.in_think = False

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while True:
            if self.in_think:
                idx = self.buf.find("</think>")
                if idx == -1:
                    safe = self._safe_cut()
                    if safe > 0:
                        self.buf = self.buf[safe:]
                    break
                self.buf = self.buf[idx + len("</think>"):]
                self.in_think = False
            else:
                idx = self.buf.find("<think>")
                if idx == -1:
                    safe = self._safe_cut()
                    if safe > 0:
                        out.append(self.buf[:safe])
                        self.buf = self.buf[safe:]
                    break
                out.append(self.buf[:idx])
                self.buf = self.buf[idx:]
                self.in_think = True
        return "".join(out)

    def _safe_cut(self):
        """可安全切走的位置：末尾若是 '<think>' 或 '</think>' 的前缀则保留。"""
        for tag in ("<think>", "</think>"):
            for n in range(min(len(self.buf), len(tag) - 1), 0, -1):
                if self.buf.endswith(tag[:n]):
                    return len(self.buf) - n
        return len(self.buf)

    def flush(self):
        return "" if self.in_think else self.buf


def translate(text, to="zh", max_tokens=400, timeout=60, host=HOST, port=PORT):
    """同步翻译，返回字符串。失败抛异常。"""
    sys_prompt = SYS_ZH if to == "zh" else SYS_EN
    # ProxyHandler({}) 显式绕 7890：否则本地 8899 也被代理劫持
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps({
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "top_p": 0.9,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/v1/chat/completions",
        data=payload, headers={"Content-Type": "application/json"})
    with op.open(req, timeout=timeout) as r:
        data = json.load(r)
    return strip_think(data["choices"][0]["message"]["content"])


def translate_stream(text, to="zh", max_tokens=400, timeout=60, host=HOST, port=PORT):
    """流式翻译，yield 每个 chunk。首字延迟约 0.5s。"""
    sys_prompt = SYS_ZH if to == "zh" else SYS_EN
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps({
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "top_p": 0.9,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/v1/chat/completions",
        data=payload, headers={"Content-Type": "application/json"})
    flt = ThinkFilter()
    with op.open(req, timeout=timeout) as r:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            delta = obj["choices"][0].get("delta", {})
            piece = delta.get("content")
            if piece:
                clean = flt.feed(piece)
                if clean:
                    yield clean
    tail = flt.flush()
    if tail:
        yield tail


def stop_server(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


# ================================================== 多引擎扩展 (本地 / 云端 / API)
# exe frozen 时 __file__ 指向 _MEIPASS 临时目录，配置读写会落到那里并在退出时丢失
# （见 AGENTS.md 规约 2.1）。这里不能复用 selection.CONFIG_PATH——selection 反过来
# import 本模块，会构成循环导入，故按同一规约就地镜像守卫。
if getattr(sys, "frozen", False):
    _APP_DIR = Path(sys.executable).resolve().parent
else:
    _APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = _APP_DIR / "config.json"


def load_trans_engine_config():
    """读取翻译引擎配置：默认云端引擎（本地翻译服务默认关闭）。"""
    defaults = {
        "trans_engine": "cloud",       # local | cloud | api
        "cloud_provider": "google",    # 兼容保留：多源接力后不再区分优先级
        "api_endpoint": "https://api.openai.com/v1",  # 标准占位符，开源版不再默认私有反代
        "api_key": "",
        "api_model": "gpt-4o-mini",
        "baidu_appid": "",             # 百度翻译开放平台（可选，配了才启用该源）
        "baidu_key": "",
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in defaults.items():
                    if k in data:
                        defaults[k] = data[k]
        except Exception:
            pass
    return defaults


def save_trans_engine_config(cfg_updates):
    """保存翻译引擎配置更新到 config.json。"""
    cur = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
    cur.update(cfg_updates)
    # 原子替换：config.json 同时被热键设置（selection.save_config）读写，
    # frozen 修复后本函数写的是同一个真实文件，截断式直写会互相写坏。
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
        return True
    except Exception as e:
        print(f"[trans_cfg] save error: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ================================================== 云端多源翻译（免 key，自动接力）
# 单一 google 端点在本机代理抖动时会 SSL EOF 被掐断（2026-09-28 用户实测：
# urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]）。云端改为多源接力：
# 依次试完全部源，任一成功即返回，全败抛聚合错误。必应 ttranslatev3 免 key
# 接口实测已被封（无论 cookie/IID/参数变体一律 statusCode 400，2026-09-28），
# 不再尝试；google 走系统代理与直连互为备份，mymemory 限 500 字符内兜底。

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _has_cjk(s):
    return any(0x4E00 <= ord(c) <= 0x9FFF or 0x3040 <= ord(c) <= 0x30FF
               or 0xAC00 <= ord(c) <= 0xD7AF for c in s)


def _gtx(text, to, use_proxy, timeout, host="translate.googleapis.com"):
    """Google 免费接口（client=gtx）。use_proxy=True 走系统代理，False 显式直连。"""
    target = "zh-CN" if to == "zh" else "en"
    q = urllib.parse.quote(text.strip())
    url = (f"https://{host}/translate_a/single"
           f"?client=gtx&sl=auto&tl={target}&dt=t&q={q}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "*/*"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({} if not use_proxy else None))
    with op.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data and data[0]:
        return "".join(p[0] for p in data[0] if p and p[0]).strip()
    return ""


def _mymemory(text, to, timeout):
    """MyMemory 翻译 API（免 key，单次限 500 字符，小文本兜底）。"""
    if len(text.strip()) > 480:
        return ""
    src = "zh-CN" if _has_cjk(text) else "en"
    dst = "en" if to == "en" else "zh-CN"
    q = urllib.parse.quote(text.strip())
    url = f"https://api.mymemory.translated.net/get?q={q}&langpair={src}|{dst}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.loads(op.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if out and "MYMEMORY WARNING" not in out and "QUERY LENGTH" not in out.upper():
        return out
    return ""


def _baidu_official(text, to, timeout):
    """百度翻译开放平台官方 API（直连，标准版每月 5 万字免费）。

    需在 fanyi-api.baidu.com 注册并把 appid/key 存进 config.json
    （baidu_appid / baidu_key）；未配置时返回空串让接力链跳过本源。
    标准版 QPS=1，并发撞限会抛错由链路兜底。"""
    cfg = load_trans_engine_config()
    appid = (cfg.get("baidu_appid") or "").strip()
    key = (cfg.get("baidu_key") or "").strip()
    if not (appid and key):
        return ""
    q = text.strip()[:2000]     # 标准版单请求 ≤6000 字节，保险截断
    salt = str(int(time.time()))
    sign = hashlib.md5((appid + q + salt + key).encode("utf-8")).hexdigest()
    body = urllib.parse.urlencode({
        "q": q, "from": "auto", "to": "zh" if to == "zh" else "en",
        "appid": appid, "salt": salt, "sign": sign,
    }).encode()
    req = urllib.request.Request(
        "https://fanyi-api.baidu.com/api/trans/vip/translate", data=body,
        headers={"User-Agent": _UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.loads(op.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    if data.get("trans_result"):
        return "\n".join(t.get("dst", "") for t in data["trans_result"]).strip()
    if data.get("error_code"):
        raise RuntimeError(f"百度{data['error_code']}: {str(data.get('error_msg', ''))[:60]}")
    return ""


def _baidu_sug(text, to, timeout):
    """百度 sug 词典接口（免 key）：英文单词/短语 → 中文释义。

    实测（2026-09-28）只对拉丁字母短输入有效（整句/中文入参返回空 data），
    所以只对 ≤40 字符的无中文文本启用；返回词典释义而非整句翻译。"""
    t = text.strip()
    if to != "zh" or len(t) > 40 or _has_cjk(t):
        return ""
    body = urllib.parse.urlencode({"kw": t}).encode()
    req = urllib.request.Request("https://fanyi.baidu.com/sug", data=body,
                                 headers={"User-Agent": _UA,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.loads(op.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    items = data.get("data") or []
    if items and data.get("errno") == 0:
        return str(items[0].get("v", "")).strip()
    return ""


def translate_cloud(text, to="zh", provider="google", timeout=8):
    """云端免费翻译：多源自动接力（免 key 源 + 可选配置源）。

    依次尝试：google(系统代理) → google(直连) → google 备用域(直连) →
    baidu 官方 API(仅当配置了 baidu_appid/baidu_key) → baidu sug 词典
    (≤40 字符英文) → mymemory(≤480字符)。任一源成功即返回；全败抛聚合错误。
    """
    if not text or not text.strip():
        return ""
    attempts = [
        ("google", lambda: _gtx(text, to, True, timeout)),
        ("google-direct", lambda: _gtx(text, to, False, timeout)),
        ("google-alt", lambda: _gtx(text, to, False, timeout,
                                    host="translate.google.com")),
        ("baidu", lambda: _baidu_official(text, to, timeout)),
        ("baidu-sug", lambda: _baidu_sug(text, to, timeout)),
        ("mymemory", lambda: _mymemory(text, to, timeout)),
    ]
    errs = []
    for name, fn in attempts:
        try:
            out = fn()
            if out:
                return out
            errs.append(f"{name}: 空结果")
        except Exception as e:
            errs.append(f"{name}: {str(e)[:80]}")
    raise RuntimeError("云端各源均失败 -> " + "; ".join(errs))


def translate_api(text, to="zh", base_url=None, api_key=None, model=None, timeout=35):
    """自定义 API 接入翻译（兼容 OpenAI / DeepSeek / CommandCode 反代）。"""
    cfg = load_trans_engine_config()
    base_url = (base_url or cfg.get("api_endpoint") or "https://api.openai.com/v1").rstrip("/")
    api_key = api_key if api_key is not None else cfg.get("api_key")
    model = model or cfg.get("api_model") or "gpt-4o-mini"

    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    sys_prompt = SYS_ZH if to == "zh" else SYS_EN
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": False,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    msg = data["choices"][0]["message"]["content"]
    return strip_think(msg)


def translate_stream_api(text, to="zh", base_url=None, api_key=None, model=None, timeout=35):
    """自定义 API 流式翻译。"""
    cfg = load_trans_engine_config()
    base_url = (base_url or cfg.get("api_endpoint") or "https://api.openai.com/v1").rstrip("/")
    api_key = api_key if api_key is not None else cfg.get("api_key")
    model = model or cfg.get("api_model") or "gpt-4o-mini"

    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    sys_prompt = SYS_ZH if to == "zh" else SYS_EN
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": True,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    flt = ThinkFilter()
    with op.open(req, timeout=timeout) as r:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            delta = obj["choices"][0].get("delta", {})
            piece = delta.get("content")
            if piece:
                clean = flt.feed(piece)
                if clean:
                    yield clean
    tail = flt.flush()
    if tail:
        yield tail


def translate_unified(text, to="zh", engine=None, **kwargs):
    """全场景统一翻译分发入口：按 engine（local/cloud/api）自动派发。"""
    cfg = load_trans_engine_config()
    eng = engine or cfg.get("trans_engine", "cloud")
    if eng == "cloud":
        return translate_cloud(text, to=to, provider=cfg.get("cloud_provider", "google"), **kwargs)
    elif eng == "api":
        return translate_api(text, to=to, **kwargs)
    else:
        # local
        return translate(text, to=to, **kwargs)


def translate_stream_unified(text, to="zh", engine=None, **kwargs):
    """全场景统一流式翻译分发入口。"""
    cfg = load_trans_engine_config()
    eng = engine or cfg.get("trans_engine", "cloud")
    if eng == "cloud":
        # 云端为瞬时返回，包装成 generator
        yield translate_cloud(text, to=to, provider=cfg.get("cloud_provider", "google"), **kwargs)
    elif eng == "api":
        for chunk in translate_stream_api(text, to=to, **kwargs):
            yield chunk
    else:
        for chunk in translate_stream(text, to=to, **kwargs):
            yield chunk


if __name__ == "__main__":
    # 命令行冒烟测试：确保 server 在跑且能翻
    print("[冒烟测试] 正在验证云端与本地接口...")
    try:
        cloud_out = translate_cloud("Hello world, AI agent is ready.", "zh")
        print(f"[云端测试成功]: {cloud_out}")
    except Exception as e:
        print(f"[云端测试异常]: {e}")

    if not port_busy():
        print("本地 8899 未监听，跳过本地冒烟")
    else:
        for src, to in [("The wound was deep.", "zh"), ("她把刀插回靴筒。", "en")]:
            t0 = time.time()
            out = translate(src, to)
            print(f"[{time.time()-t0:.2f}s] {to}: {out}")

