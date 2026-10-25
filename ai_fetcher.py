# -*- coding: utf-8 -*-
"""AI 专区真实数据抓取器 (ai_fetcher.py)

新闻：6 个 RSS 源（机器之心 / 量子位 / TechCrunch AI / The Verge AI /
      HNRSS / arXiv cs.AI），抓前 24 小时条目（模型发布/前沿论文类限近 10 天），关键词自动分类，
      跨源标题去重，写入 ai_news_cache.json。
排行榜：HF API（下载量 / 开源生态 / 代码模型热度）+ OpenRouter
      models API（价格与上下文窗口）+ AA + Arena + LiveBench → 本地缓存。
AA 原始行派生八维能力榜与 AA 对比目录；Arena 与 LiveBench 各有独立对比目录。
调度：daemon 线程，每天 19:00 抓取（含 19 点后每小时补偿重试），
      启动时缓存超过 20 小时则先补一次。
零第三方依赖：urllib + xml.etree，RSS 2.0 / Atom / RDF 三格式通吃。
"""
import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib import request as urlreq
from urllib.parse import urlencode, urlparse

import ai_media_rankings

# onefile exe 里 __file__ 指向 _MEIPASS 临时解压目录，缓存读写必须落
# exe 同目录（否则每日新闻重启即丢，2026-09-19 实测踩坑）
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
NEWS_CACHE = BASE_DIR / "ai_news_cache.json"
RANK_CACHE = BASE_DIR / "ai_rankings_cache.json"
MODELS_CACHE = BASE_DIR / "ai_models_cache.json"      # 模型对比目录（按数据源分区）
BRIEF_CACHE = BASE_DIR / "ai_briefings_cache.json"    # 第三方每日要闻
# 媒体榜（视频/生图/音频）的模型资料缓存。**与对比目录缓存分开**：
# 参数量/许可属于「模型资料」，混进 ai_models_cache.json 会污染对比目录。
MEDIA_METADATA_CACHE = BASE_DIR / "ai_media_metadata_cache.json"

FETCH_HOUR = 19          # 每天 19:00 更新
STALE_HOURS = 20         # 启动补偿：缓存超过 20 小时就补抓
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) XiaoyuToolbox/2.1"

NEWS_SOURCES = [
    # (站点名, RSS 地址, 该源的默认分类兜底)
    # 2026-09-19 收源：砍掉 Hacker News（内容太杂，军事/金融/就业全混入，
    # 关键词分类压不住）与 arXiv cs.AI（与 HF Daily Papers 重复且摘要差）。
    # 主力研究源锁定 HuggingFace Daily Papers（fetch_hf_daily_papers 专用抓取，
    # 不走 RSS 解析，每日必更、社区热度精选，业界日报项目公认的每日核心源）。
    ("机器之心", "https://www.jiqizhixin.com/rss", "industry"),
    ("量子位", "https://www.qbitai.com/feed", "industry"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", "industry"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "industry"),
    ("HuggingFace Papers", "", "research"),
]

# 只保留有判别力的强信号词（2026-09-19 实测踩坑：model/api/code/agent/llm/
# 模型名等高频泛词几乎命中所有 AI 新闻，先到先得导致 models/dev 通吃、
# 分类全乱）；标题无命中再试摘要，仍无命中走源默认兜底。
MODELS_KW = ["发布", "上新", "开源", "旗舰", "weights", "open-weight",
             "unveil", "releases", "launches", "open-sourc", "debut",
             "推出", "上线"]
DEV_KW = ["编程", "编码", "代码生成", "coding", "cli", "ide", "copilot",
          "cursor", "开发者", "developer", "编程助手", "编程 agent",
          "coding agent"]
RESEARCH_KW = ["论文", "paper", "arxiv", "benchmark", "评测", "scaling",
               "训练", "dataset", "模型架构", "推理能力", "study finds"]
INDUSTRY_KW = ["融资", "收购", "估值", "监管", "起诉", "裁员", "芯片",
               "算力", "数据中心", "funding", "acquisition", "regulation",
               "lawsuit", "sues", "layoff", "chip", "data center",
               "ipo", "market", "禁止", "ban"]


def _http_get(url, timeout=12):
    req = urlreq.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    op = urlreq.build_opener(urlreq.ProxyHandler(urlreq.getproxies()))
    with op.open(req, timeout=timeout) as r:
        return r.read()


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_time(raw):
    """尽量解析 RFC822 / ISO 时间，失败返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(datetime.now().strftime(fmt))],
                                     fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _classify(title, summary, source_default):
    # 标题是编辑意图所在：先只扫标题，无命中再扫摘要（降低摘要泛词误伤）
    for text in ((title or ""), (title or "") + " " + (summary or "")):
        text = text.lower()
        for kw in INDUSTRY_KW:
            if kw in text:
                return "industry"
        for kw in MODELS_KW:
            if kw in text:
                return "models"
        for kw in DEV_KW:
            if kw in text:
                return "dev"
        if source_default == "research":
            return "research"
        for kw in RESEARCH_KW:
            if kw in text:
                return "research"
    return source_default


CATEGORY_NAMES = {"models": "模型发布", "dev": "编程 Agent",
                  "research": "前沿论文", "industry": "行业动态"}


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _feed_entry_image(el):
    """从 feed 条目里抠一张内嵌配图（正文 <img>、media:content/thumbnail、enclosure）。

    各源行为不一（2026-10-10 实测）：The Verge 的 Atom content 里是不转义 HTML，
    <img> 解析即得；media:* / enclosure 用 url 属性带图，按有无 url 与 type 区分于
    正文 content；机器之心/量子位/TechCrunch 的 feed 完全不带图，返回空串，由
    _enrich_images 抓文章页 og:image 兜底。
    """
    for sub in el.iter():
        tag = _local_name(sub.tag)
        if tag == "img":
            src = (sub.get("src") or "").strip()
            if src.startswith("http"):
                return src
        elif tag in ("thumbnail", "enclosure") or (tag == "content" and sub.get("url")):
            ctype = (sub.get("type") or "").lower()
            src = (sub.get("url") or "").strip()
            if src.startswith("http") and (not ctype or "image" in ctype):
                return src
    return ""


def _parse_feed(xml_bytes, source_name, source_default):
    """兼容 RSS 2.0 / Atom / RDF 三种格式，返回原始条目 (title, link, summary, pub, image)。"""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items
    for el in root.iter():
        tag = _local_name(el.tag)
        if tag != "item" and tag != "entry":
            continue
        title, link, summary, pub = "", "", "", None
        for child in el.iter():
            c = _local_name(child.tag)
            if c == "title" and not title:
                title = _strip_html(child.text or "")
            elif c in ("link",) and not link:
                link = (child.get("href") or (child.text or "").strip())
            elif c in ("description", "summary", "content") and not summary:
                summary = _strip_html(child.text or "")[:220]
            elif c in ("pubDate", "published", "updated", "date") and pub is None:
                pub = _parse_time(child.text or "")
        if title:
            items.append((title, link, summary, pub, _feed_entry_image(el)))
    return items


def fetch_hf_daily_papers():
    """抓 HuggingFace Daily Papers（每日精选，社区热度排序）。

    主力研究源：每日必更、逐篇天然成卡，业界 AI 日报项目的公认核心源。
    页面 SSR，论文数据以内嵌 JSON 存在 data-props 属性（&quot; 转义），
    含 title/summary/publishedAt/organization 全字段。注意：dailyPapers
    是"今日精选"清单，论文发表日期可能早于今日 → 打 _force 标记豁免
    时间窗过滤，否则会被 24h 窗口全部滤掉。
    """
    import html as html_mod
    raw = _http_get(HF_PAPERS_URL, timeout=20).decode("utf-8", "replace")
    for m in re.finditer(r'data-props="([^"]{200,})"', raw):
        try:
            data = json.loads(html_mod.unescape(m.group(1)))
        except Exception:
            continue
        papers = data.get("dailyPapers") if isinstance(data, dict) else None
        if not papers:
            continue
        out = []
        for p in papers:
            title = _strip_html(p.get("title") or "")
            if not title:
                continue
            org = (p.get("organization") or {})
            org_name = org.get("fullname") or org.get("name") or ""
            pid = (p.get("paper") or {}).get("id") or ""
            # 帖子卡配图：mediaUrls 是作者上传的图表/视频，只认图片扩展名；
            # 空缺时前端走无图占位，不去盲拼 HF 缩略图 URL（2026-10-10 实测 404）
            _img = next((u for u in ((p.get("paper") or {}).get("mediaUrls") or [])
                         if isinstance(u, str) and u.lower().split("?")[0].endswith(
                             (".jpg", ".jpeg", ".png", ".webp"))), "")
            out.append({
                "title": title[:150],
                "summary": _strip_html(p.get("summary") or "")[:220],
                # 论文固定归"前沿论文"——实测论文英文摘要里 coding/developers
                # 等词频太高，按关键词细分反而把 KV 缓存压缩这类论文误判进 dev
                "category": "research",
                "source": "HuggingFace Papers",
                "source_url": (f"https://huggingface.co/papers/{pid}"
                               if pid else HF_PAPERS_URL),
                "_pub": _parse_time(p.get("publishedAt") or ""),
                "_heat": int((p.get("paper") or {}).get("upvotes") or 0),
                "_org": _strip_html(org_name),
                "_force": True,
                "_img": _img,
            })
        # 页面顺序即热度序。dailyPapers 会带近期积累（2026-10-04 实测 58 条），
        # _force 又豁免时间窗，若不限流，60 条配额被 HF+V2EX 吃满、业界 RSS
        # 全部垫底被截断（缓存实测 RSS=0）——保留票数最高的前 20 条。
        return out[:20]
    return []


def fetch_news():
    """抓取全部源，返回 (条目列表, 成功源数)。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    fallback_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    all_items, ok_sources = [], 0
    for source_name, url, default_cat in NEWS_SOURCES:
        if not url:                    # HuggingFace Papers：专用抓取器
            continue
        try:
            raw = _parse_feed(_http_get(url), source_name, default_cat)
        except Exception as e:
            print(f"[ai_fetcher] {source_name} 抓取失败: {e}", flush=True)
            continue
        ok_sources += 1
        for title, link, summary, pub, image in raw:
            if not summary:
                summary = title[:120]
            all_items.append({
                "title": title[:150],
                "summary": summary,
                "category": _classify(title, summary, default_cat),
                "source": source_name,
                "source_url": link,
                "_pub": pub,
                "_img": image,
            })
    # 主力研究源：HuggingFace Daily Papers
    try:
        hf_items = fetch_hf_daily_papers()
        if hf_items:
            all_items.extend(hf_items)
            ok_sources += 1
    except Exception as e:
        print(f"[ai_fetcher] HuggingFace Papers 抓取失败: {e}", flush=True)
    # 社区热点：V2EX（Linux.do 硬防抓不了，见 V2EX_NODES 注释）
    try:
        v2_items = fetch_v2ex_hot()
        if v2_items:
            all_items.extend(v2_items)
            ok_sources += 1
    except Exception as e:
        print(f"[ai_fetcher] V2EX 抓取失败: {e}", flush=True)
    # 时间窗：优先 24h；不足 24 条放宽到 48h（HF 限流后总池变小，
    # 阈值从 8 提到 24 维持快讯流丰满度）
    def in_window(it, since):
        if it.get("_force"):           # HF 今日精选：豁免时间窗
            return True
        return it["_pub"] is not None and it["_pub"] >= since
    windowed = [i for i in all_items if in_window(i, cutoff)]
    if len(windowed) < 24:
        windowed = [i for i in all_items if in_window(i, fallback_cutoff)] or \
                   [i for i in all_items if i["_pub"] is not None]
    # 排序：热度优先（2026-09-19 用户要求），同分内时间倒序。
    # 热度信号：HF upvotes / V2EX 回复数，log 压缩统一量纲（高票领跑、
    # 低票不垫底）；RSS 源无热度数据给保底分 2，避免行业动态全部沉底；
    # 时间加成 6h 内最高 +6，之后线性衰减到 36h 归零（新鲜度不丢）。
    def hot_score(it):
        score = it.get("_heat")
        score = math.log10(1 + score) * 8 if score else 2
        pub = it["_pub"]
        if pub:
            hours = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
            score += max(0.0, 6 - hours / 6)
        return score

    windowed.sort(key=lambda x: (hot_score(x),
                                 x["_pub"] or datetime.min.replace(tzinfo=timezone.utc)),
                  reverse=True)
    seen, dedup = set(), []
    for it in windowed:
        key = re.sub(r"\W+", "", it["title"].lower())[:28]
        if key and key not in seen:
            seen.add(key)
            dedup.append(it)
    # 「模型发布 / 前沿论文」只留近 10 天（其余分类维持原时间窗逻辑）
    ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
    dedup = [it for it in dedup
             if it.get("category") not in ("models", "research")
             or it.get("_pub") is None or it["_pub"] >= ten_days_ago]
    # 输出为前端契约字段
    now = datetime.now(timezone.utc)
    out = []
    for i, it in enumerate(dedup[:60]):
        pub = it["_pub"] or now
        local = pub.astimezone()
        hot = i < 6                          # 热度排序后的前 6 条挂 🔥
        out.append({
            "id": f"news-{local.strftime('%Y%m%d%H%M')}-{i}",
            "title": it["title"],
            "summary": it["summary"],
            "category": it["category"],
            "category_name": CATEGORY_NAMES.get(it["category"], "行业动态"),
            "source": it["source"],
            "source_url": it["source_url"],
            "published_at": local.strftime("%Y-%m-%d %H:%M"),
            "tags": [t for t in (it["source"],
                                 CATEGORY_NAMES.get(it["category"], ""),
                                 it.get("_org") or "") if t],
            "heat": round(hot_score(it), 1),
            "hot": hot,
            "image": it.get("_img") or "",
        })
    # 帖子化配图：feed 内嵌图优先，无图条目抓文章页 og:image 兜底（只补前 36 条，
    # 尾部旧条目不值得每条一次外站请求；V2EX 帖子页没图，_og_image_of 会跳过）
    _enrich_images(out, "source_url", limit=36)
    # 英文源条目批量译中（写入缓存，前端优先显示；失败保留英文不阻塞）
    _batch_translate_zh(out)
    return out, ok_sources


def _has_cjk(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text))


# \u65b0\u95fb\u7ffb\u8bd1\u7684\u5f15\u64ce\u515c\u5e95\u987a\u5e8f\uff08\u7528\u6237\u914d\u7f6e\u7684\u5f15\u64ce\u59cb\u7ec8\u6392\u7b2c\u4e00\uff0c\u89c1 _translate_zh\uff09
_TRANS_FALLBACK = ("local", "api", "cloud")


def _translate_zh(text, configured=None):
    """单条英译中：先走用户配置的引擎，再按 local → api → cloud 兜底。

    **不能写死 cloud**：Google gtx 免费接口会 429，且实测被封后**持续**失败
    （间隔单发也全 429，不是并发触发的瞬时限流），而 _batch_translate_zh 原本
    「单条失败静默保留英文」——于是新闻缓存里一条 title_zh 都没有、前端整屏英文、
    日志毫无痕迹，2026-09-22 用户报「新闻没有翻译」才暴露。

    中文原文直接返回 None（不重复翻）；全部引擎都失败才返回 None，调用方据此保留英文。
    """
    if not text or _has_cjk(text):
        return None
    try:
        from translate_backend import translate_unified, load_trans_engine_config
    except Exception:
        return None
    if configured is None:
        try:
            configured = load_trans_engine_config().get("trans_engine") or "cloud"
        except Exception:
            configured = "cloud"
    order = (configured,) + tuple(e for e in _TRANS_FALLBACK if e != configured)
    for eng in order:
        try:
            zh = translate_unified(text, to="zh", engine=eng, timeout=25)
        except Exception:
            continue
        # 结果里没有中文说明引擎回吐了原文（接口异常时会这样），当失败继续兜底
        if zh and _has_cjk(zh):
            return zh
    return None


def _batch_translate_zh(items, max_workers=3):
    """并发补齐条目的 title_zh / summary_zh 字段。

    中文源（机器之心/量子位）自动跳过。**并发压到 3**：兜底链路里的 local 引擎是
    单实例 llama-server，6 并发会把请求排队到超时；cloud 引擎并发高还招 429。
    失败必须吵出来——这个函数以前完全静默，正是整屏英文却查无实据的原因。
    """
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for it in items:
        for field in ("title", "summary"):
            t = it.get(field) or ""
            if t and not _has_cjk(t):
                jobs.append((it, field, t))
    if not jobs:
        return
    try:
        import translate_backend
        configured = translate_backend.load_trans_engine_config().get("trans_engine") or "cloud"
    except Exception:
        configured = "cloud"
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_translate_zh, t, configured): (it, field)
                for it, field, t in jobs}
        for fu, (it, field) in futs.items():
            try:
                zh = fu.result()
            except Exception:
                zh = None
            if zh:
                it[field + "_zh"] = zh
                ok += 1
    if ok == 0:
        print(f"[ai_fetcher] 新闻翻译全部失败（{len(jobs)} 条字段），本期保留英文原文"
              f"（配置引擎={configured}，兜底 {_TRANS_FALLBACK} 均不可用）", flush=True)
    elif ok < len(jobs):
        print(f"[ai_fetcher] 新闻翻译部分成功 {ok}/{len(jobs)}（配置引擎={configured}）",
              flush=True)
    else:
        print(f"[ai_fetcher] 新闻翻译完成 {ok}/{len(jobs)}（配置引擎={configured}）",
              flush=True)


# ================================================== 帖子配图（og:image）
_OG_IMAGE_TAG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]*>', re.I)
_OG_IMAGE_TAG_RE_REV = re.compile(
    r'<meta[^>]+content=["\'][^"\']+["\'][^>]*(?:property|name)=["\']og:image["\'][^>]*>', re.I)
# 站点 logo 也会被写成 og:image（2026-10-10 实测量子位文章页 og:image 就是
# qbitai-logo），这类 URL 直接当无图处理
_OG_IMAGE_JUNK_RE = re.compile(
    r"logo|sprite|favicon|icon|avatar|spinner|placeholder", re.I)


def _og_image_of(url):
    """抓文章页 og:image；best effort——失败一律返回 None，调用方按无图处理。

    快讯/要闻帖子化的主图源：多数 feed 不带内嵌图（5 源实测仅 The Verge 带），
    但 TechCrunch / The Verge / the-decoder / HF 论文页 / X 帖子页都给 og:image
    （X 对爬虫回 pbs.twimg.com 的帖子图）。ithome 之类 JS 渲染站抓不到就留空。
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    if "v2ex.com" in urlparse(url).netloc.lower():
        return None                       # 帖子页无 og:image，省一次必空请求
    try:
        page = _http_get(url, timeout=10).decode("utf-8", "replace")[:300000]
    except Exception:
        return None
    m = _OG_IMAGE_TAG_RE.search(page) or _OG_IMAGE_TAG_RE_REV.search(page)
    if not m:
        return None
    c = re.search(r'content=["\']([^"\']+)["\']', m.group(0), re.I)
    if not c:
        return None
    import html as html_mod
    img = html_mod.unescape(c.group(1)).strip()
    if img.startswith("http") and not _OG_IMAGE_JUNK_RE.search(img):
        _register_image_host(img)        # 登记图床域名，fetch_remote_image 放行
        return img
    return None


def _enrich_images(items, url_key, image_key="image", limit=None, max_workers=6):
    """给无图条目并发补抓 og:image（快讯条目用）。

    失败静默：无图条目前端渲染无图占位，不是错误。limit 限制补抓条数——
    尾部旧条目首屏看不到，不值得每条一次外站请求。
    """
    todo = [it for it in (items if limit is None else items[:limit])
            if not (it.get(image_key) or "")]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_og_image_of, it.get(url_key) or ""): it for it in todo}
        for fu, it in futs.items():
            img = None
            try:
                img = fu.result()
            except Exception:
                pass
            if img:
                it[image_key] = img
                ok += 1
    print(f"[ai_fetcher] og:image 补抓 {ok}/{len(todo)} 条", flush=True)


def _enrich_briefing_images(briefings, latest_days=2, max_workers=6):
    """给最新几期要闻的每个 section（帖子卡）补抓配图。

    要闻源条目本身不带图（aihot 是"精选条目流"，llmpk 只给期封面），逐节取
    「阅读原文」外链抓 og:image（aihot 自己的页没图，跳过；X 链接实测可抓）。
    只补最新 latest_days 期——旧期读者很少翻到，省一轮外站请求。
    """
    jobs = []
    for b in briefings[:latest_days]:
        for s in (b.get("sections") or []):
            orig = next((l["url"] for l in (s.get("links") or [])
                         if l.get("url")), "")
            if orig:
                jobs.append((s, orig))
    if not jobs:
        return
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_og_image_of, u): s for s, u in jobs}
        for fu, s in futs.items():
            img = None
            try:
                img = fu.result()
            except Exception:
                pass
            if img:
                s["image"] = img
                ok += 1
    print(f"[ai_fetcher] 要闻帖子配图补抓 {ok}/{len(jobs)} 节", flush=True)


# ================================================== 排行榜真实源
HF_API = "https://huggingface.co/api/models?filter=text-generation&sort=downloads&limit=200&full=false"
OPENROUTER_API = "https://openrouter.ai/api/v1/models"
DATALEARNER_URL = "https://www.datalearner.com/leaderboards"
HF_PAPERS_URL = "https://huggingface.co/papers"
V2EX_HOT_URL = "https://www.v2ex.com/api/topics/hot.json"
# Linux.do 有 Cloudflare 硬防（匿名 JSON/RSS/HTML 全 403，2026-09-19 实测），
# 无头过盾违反轻量红线、塞 cookie 脆且有账号安全风险——放弃。
# V2EX 气质相近且公开可抓，只留技术开发节点的热帖。
V2EX_NODES = ("程序员", "人工智能", "机器学习", "深度学习", "大模型",
              "开源", "分享创造", "算法", "云计算", "数据库", "Python",
              "Node.js", "Go", "Rust", "前端", "后端", "Linux", "Windows",
              "macOS", "硬件", "AI", "奇思妙想")
# 注意：白名单刻意不含"推广"节点——实测热榜第一条是 API 倒卖广告帖，
# 会霸占热度前排（2026-09-19）。


def fetch_v2ex_hot():
    """抓 V2EX 热点（中文开发者社区，公开 JSON API 免 key）。

    全站热帖混有职场/生活杂谈，按节点白名单只留技术开发类；
    兜底分类 dev。热帖按社区热度而非发表时间排序 → _force 豁免时间窗。
    """
    raw = _http_get(V2EX_HOT_URL, timeout=12).decode("utf-8", "replace")
    out = []
    for t in json.loads(raw):
        node = ((t.get("node") or {}).get("title")) or ""
        if node not in V2EX_NODES:
            continue
        title = _strip_html(t.get("title") or "")
        if not title:
            continue
        content = _strip_html(t.get("content") or "")
        created = t.get("created")
        out.append({
            "title": title[:150],
            "summary": (content or title)[:220],
            "category": _classify(title, content, "dev"),
            "source": "V2EX 热点",
            "source_url": t.get("url") or "https://www.v2ex.com/",
            "_pub": _parse_time(time.strftime(
                "%Y-%m-%d %H:%M:%S", time.gmtime(created))
                if created else ""),
            "_heat": int(t.get("replies") or 0),
            "_force": True,
        })
    return out


AA_MODELS_URL = "https://artificialanalysis.ai/leaderboards/models"
LMARENA_TEXT_URL = "https://lmarena.ai/leaderboard/text"
ARENA_DATASET = "lmarena-ai/leaderboard-dataset"
ARENA_DATASET_API = "https://datasets-server.huggingface.co"
LIVEBENCH_REPO = "https://raw.githubusercontent.com/LiveBench/new-livebench/main"
LIVEBENCH_CONSTANTS_URL = LIVEBENCH_REPO + "/src/lib/constants.js"
LIVEBENCH_MODEL_LINKS_URL = LIVEBENCH_REPO + "/src/Table/modelLinks.js"


_DATASET_API_LOCK = threading.Lock()
_DATASET_API_LAST_REQUEST_AT = 0.0
_DATASET_API_MIN_INTERVAL = 0.5
_DATASET_API_MAX_RETRY_AFTER = 120


def _dataset_retry_after_seconds(error):
    headers = getattr(error, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except Exception:
            return None


def _dataset_api_json(url):
    """串行限速 Hugging Face Dataset Viewer 请求，并按 Retry-After 退避重试 429。"""
    global _DATASET_API_LAST_REQUEST_AT
    for attempt in range(3):
        try:
            with _DATASET_API_LOCK:
                gap = _DATASET_API_MIN_INTERVAL - (
                    time.monotonic() - _DATASET_API_LAST_REQUEST_AT)
                if gap > 0:
                    time.sleep(gap)
                try:
                    raw = _http_get(url, timeout=30)
                finally:
                    _DATASET_API_LAST_REQUEST_AT = time.monotonic()
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:
            status = getattr(e, "code", None)
            if attempt >= 2 or (status is not None and status < 500 and status != 429):
                raise
            if status == 429:
                delay = _dataset_retry_after_seconds(e)
                if delay is not None:
                    # 超长冷却不阻塞整轮刷新；留待下次手动/定时刷新。
                    if delay > _DATASET_API_MAX_RETRY_AFTER:
                        raise
                else:
                    delay = 5 * (2 ** attempt)
            else:
                delay = min(30, 2 ** (attempt + 1))
            time.sleep(max(0.5, delay))
    raise RuntimeError("Hugging Face Dataset Viewer 请求失败")


# AA 榜单页内嵌 rows 的分维字段（2026-09-21 实测确认，勿臆造字段名）。
# 三个坑：
# ① 除 scale=elo 与已百分化的字段外，原始值都是 0–1 分数，展示前要 ×100；
# ② `omniscience` 是「准确率 − 幻觉惩罚」的带符号综合指数（实测 -59 ~ +43），
#    **不是**准确率——知识准确率要用 `omniscienceAccuracy`，选错字段会出一屏负数；
# ③ GDPval 的官方列名写明标度是 `(Elo-500)/2000`，换算回 Elo 才有可比性。
# label / english 取自页面内嵌的官方 columns 字典，不自造名词。
AA_CAP_DIMS = (
    {"key": "gdpval", "field": "gdpvalNormalized",
     "label": "真实世界工作任务", "english": "Agentic Real-World Work Tasks",
     "scale": "elo",
     "desc": "投行/咨询/法律等真实交付物任务上的人工对照评审，页面原始标度是 "
             "(Elo-500)/2000，此处换算回 Elo 展示。"},
    {"key": "terminal", "field": "terminalBench21",
     "label": "终端与编码智能体", "english": "Agentic Coding & Terminal Use",
     "scale": "percent",
     "desc": "Terminal-Bench v2.1：在真实终端环境里完成多步编码运维任务的成功率。"},
    {"key": "scicode", "field": "scicode",
     "label": "科研编程", "english": "SciCode",
     "scale": "percent",
     "desc": "把科研问题转成可执行代码并复现数值结果，偏科学与工程计算。"},
    {"key": "lcr", "field": "lcr",
     "label": "长上下文推理", "english": "Long Context Reasoning",
     "scale": "percent",
     "desc": "AA-LCR：长文档中跨段落定位、聚合与推理的能力。"},
    {"key": "hle", "field": "hle",
     "label": "推理与知识", "english": "Reasoning & Knowledge",
     "scale": "percent",
     "desc": "Humanity's Last Exam：高难度跨学科专家级题目。"},
    {"key": "gpqa", "field": "gpqa",
     "label": "科学推理", "english": "Scientific Reasoning",
     "scale": "percent",
     "desc": "GPQA Diamond：研究生水平的生物/物理/化学题，谷歌不出的难题。"},
    {"key": "critpt", "field": "critpt",
     "label": "批判性推理", "english": "CritPT",
     "scale": "percent",
     "desc": "对论证的前提、漏洞与反例做批判性审查的能力。"},
    {"key": "omni", "field": "omniscienceAccuracy",
     "label": "知识准确率", "english": "AA-Omniscience",
     "scale": "percent",
     "desc": "知识问答的准确率（不含幻觉惩罚项，与 AA 的综合指数口径不同）。"},
)

_RSC_PUSH_RE = re.compile(r'self\.__next_f\.push\((\[1,".*?"\])\)</script>', re.S)


def _rsc_stream(url, timeout=30):
    """Next.js App Router 页面：把 self.__next_f RSC 流拼成可扫描的大字符串。"""
    html = _http_get(url, timeout=timeout).decode("utf-8", "replace")
    parts = []
    for m in _RSC_PUSH_RE.finditer(html):
        try:
            parts.append(json.loads(m.group(1))[1])
        except Exception:
            pass
    return "".join(parts)


def _rsc_arrays(stream, key):
    """流里所有『首元素含指定字段的对象数组』，取最长的一个。"""
    dec = json.JSONDecoder()
    best = []
    for m in re.finditer(r'\[\{"', stream):
        try:
            val, _ = dec.raw_decode(stream, m.start())
        except Exception:
            continue
        if (isinstance(val, list) and val and isinstance(val[0], dict)
                and key in val[0] and len(val) > len(best)):
            best = val
    return best


def _fetch_aa_rows():
    """AA 官方榜原始 rows（约 650 条，含全部分维/价格/速度字段）。

    抽成独立函数是为了三处复用同一份数据（智能指数榜 / 八维能力榜 /
    模型对比目录）——一次网络请求喂三份产出，避免重复拉 1.1MB 的 RSC 流。
    """
    rows = _rsc_arrays(_rsc_stream(AA_MODELS_URL), "intelligenceIndex")
    if len(rows) < 10:
        raise RuntimeError("AA 页面 rows 解析过少（可能改版）")
    return rows


def _fetch_aa_index(rows=None):
    """Artificial Analysis 官方榜：智能指数全表取前 100。

    rows 传 `_fetch_aa_rows()` 的结果可复用同一份抓取（见 fetch_rankings）。
    """
    if rows is None:
        rows = _fetch_aa_rows()
    live = [m for m in rows
            if not m.get("deprecated") and isinstance(m.get("intelligenceIndex"), (int, float))]
    live.sort(key=lambda m: -m["intelligenceIndex"])
    out = []
    for i, m in enumerate(live[:100]):
        out.append({
            "rank": i + 1,
            "model": m.get("shortName") or m.get("name") or "--",
            "org": m.get("modelCreatorName") or "--",
            "score": round(m["intelligenceIndex"], 1),
            "thinking": "深度思考" if m.get("isReasoning") else "-",
            "license": "开源" if m.get("isOpenWeights") else "闭源",
            "badge": "综合能力第一" if i == 0 else "",
        })
    if len(out) < 10:
        raise RuntimeError("AA 页面 rows 解析过少（可能改版）")
    return out


def _aa_cap_value(scale, raw):
    """AA 原始分数 → 展示值。percent：0–1 分数转百分数；elo：GDPval 标度还原成 Elo。"""
    v = float(raw)
    return round(v * 2000 + 500) if scale == "elo" else round(v * 100, 1)


def _capability_boards(rows, today):
    """八维能力榜：每个维度各取有值的前 100。

    同一模型的不同思考档位（xhigh/max/low…）是 AA 里的独立条目，与现有
    智能指数榜、LMArena 榜口径一致，不额外合并。某维度有值条目不足 10 条
    判定为页面改版，跳过该榜而不是出一张残缺表。
    """
    live = [m for m in rows if not m.get("deprecated")]
    out = {}
    for dim in AA_CAP_DIMS:
        field = dim["field"]
        scored = [m for m in live if isinstance(m.get(field), (int, float))]
        if len(scored) < 10:
            print(f"[ai_fetcher] 能力维度 {dim['key']} 有值条目过少"
                  f"（{len(scored)}），跳过", flush=True)
            continue
        scored.sort(key=lambda m: -m[field])
        items = []
        for i, m in enumerate(scored[:100]):
            items.append({
                "rank": i + 1,
                "model": m.get("shortName") or m.get("name") or "--",
                "org": m.get("modelCreatorName") or "--",
                "value": _aa_cap_value(dim["scale"], m[field]),
                "unit": "Elo" if dim["scale"] == "elo" else "%",
                "license": "开源" if m.get("isOpenWeights") else "闭源",
                "badge": "该维度第一" if i == 0 else "",
            })
        out["cap_" + dim["key"]] = {
            "name": f"能力榜 · {dim['label']}",
            "benchmark": f"Artificial Analysis · {dim['english']}（官方直取）",
            "description": dim["desc"],
            "updated_at": today, "items": items, "source": "live",
        }
    return out


def build_model_catalog(rows, today):
    """模型对比目录：AA 原始行压成紧凑记录，供前端做「模型对比」。

    只收至少有一项能力分的模型——一条分数都没有的行进不了对比表。
    """
    models = []
    for m in rows:
        if m.get("deprecated"):
            continue
        cap = {}
        for dim in AA_CAP_DIMS:
            v = m.get(dim["field"])
            if isinstance(v, (int, float)):
                cap[dim["key"]] = _aa_cap_value(dim["scale"], v)
        if not cap:
            continue
        models.append({
            "name": m.get("shortName") or m.get("name") or "--",
            "org": m.get("modelCreatorName") or "--",
            "open_weights": bool(m.get("isOpenWeights")),
            "reasoning": bool(m.get("isReasoning")),
            "intelligence": (round(m["intelligenceIndex"], 1)
                             if isinstance(m.get("intelligenceIndex"), (int, float))
                             else None),
            "context": m.get("contextWindowTokens"),
            "price_in": m.get("price1mInputTokens"),
            "price_out": m.get("price1mOutputTokens"),
            "speed": (round(m["medianOutputTokensPerSecond"], 1)
                      if isinstance(m.get("medianOutputTokensPerSecond"), (int, float))
                      else None),
            "cap": cap,
        })
    models.sort(key=lambda r: -(r["intelligence"]
                                if r["intelligence"] is not None else -1e9))
    return {
        "updated_at": today,
        "dimensions": [{"key": d["key"], "label": d["label"], "english": d["english"],
                        "unit": "Elo" if d["scale"] == "elo" else "%"}
                       for d in AA_CAP_DIMS],
        "models": models,
    }


def _fetch_arena_direct():
    """LMArena 官方 text 榜：Elo 全表（约 400 模型）。"""
    stream = _rsc_stream(LMARENA_TEXT_URL)
    rows = _rsc_arrays(stream, "rating")
    rows = [r for r in rows if isinstance(r.get("rating"), (int, float))]
    rows.sort(key=lambda r: r.get("rank") or 10**9)
    out = []
    for i, r in enumerate(rows[:100]):
        ci = r.get("ratingUpper", 0) - r.get("rating", 0)
        out.append({
            "rank": r.get("rank") or i + 1,
            "model": r.get("modelDisplayName") or r.get("modelKey") or "--",
            "org": r.get("modelOrganization") or "--",
            "elo": round(r["rating"]),
            "ci": "+/-%d" % round(ci),
            "votes": _fmt_compact(r.get("votes")),
            "license": r.get("license") or "-",
            "badge": "人类偏好第一" if i == 0 else "",
        })
    if len(out) < 10:
        raise RuntimeError("LMArena 页面 rows 解析过少（可能改版）")
    return out


def _slug_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_") or "unknown"


ARENA_CONFIG_LABELS = {
    "text": "文本",
    "text_style_control": "文本（风格控制）",
    "text_factuality": "文本事实性",
    "vision": "视觉",
    "vision_style_control": "视觉（风格控制）",
    "search": "搜索",
    "search_style_control": "搜索（风格控制）",
    "search_factuality": "搜索事实性",
    "document": "文档",
    "document_style_control": "文档（风格控制）",
    "webdev": "代码与 WebDev",
    "text_to_image": "文生图",
    "image_edit": "图像编辑",
    "text_to_video": "文生视频",
    "image_to_video": "图生视频",
    "video_edit": "视频编辑",
    "agent": "Agent",
    "agent_bash_recovery_steps": "Agent · Bash 恢复",
    "agent_praise_complaint": "Agent · 用户反馈",
    "agent_steerability": "Agent · 可控性",
    "agent_task_outcome_explicit": "Agent · 任务完成",
    "agent_tool_hallucination": "Agent · 工具幻觉",
}

ARENA_CATEGORY_LABELS = {
    "overall": "综合",
    "expert": "专家题",
    "hard_prompts": "高难提示",
    "coding": "编程",
    "math": "数学",
    "creative_writing": "创意写作",
    "instruction_following": "指令遵循",
    "longer_query": "长问题",
}


def _fetch_arena_dataset_rows():
    """从 Arena 官方 Hugging Face 数据集读取所有 latest 配置，不拉 full 历史集。"""
    split_url = ARENA_DATASET_API + "/splits?" + urlencode({"dataset": ARENA_DATASET})
    split_data = _dataset_api_json(split_url)
    configs = sorted({s.get("config") for s in split_data.get("splits", [])
                      if s.get("split") == "latest" and s.get("config")})
    if not configs:
        raise RuntimeError("Arena 数据集没有可用的 latest 配置")

    by_config = {}
    errors = {}
    for config in configs:
        rows, offset, total = [], 0, None
        try:
            while total is None or offset < total:
                params = {"dataset": ARENA_DATASET, "config": config,
                          "split": "latest", "offset": offset, "length": 100}
                url = ARENA_DATASET_API + "/rows?" + urlencode(params)
                page = _dataset_api_json(url)
                total = int(page.get("num_rows_total") or 0)
                page_rows = page.get("rows") or []
                for wrapped in page_rows:
                    row = wrapped.get("row") if isinstance(wrapped, dict) else None
                    if not isinstance(row, dict):
                        continue
                    model = str(row.get("model_name") or "").strip()
                    category = str(row.get("category") or "overall").strip()
                    score = row.get("rating")
                    if score is None:
                        score = row.get("score")
                    if (model and isinstance(score, (int, float))
                            and math.isfinite(float(score))):
                        rows.append(row)
                if not page_rows or len(page_rows) < 100:
                    break
                offset += len(page_rows)
            if rows:
                # 模型可在不同 category 出现，但同一榜内同模型同类别只能有一行。
                seen = set()
                for row in rows:
                    key = (row.get("model_name"), row.get("category") or "overall")
                    if key in seen:
                        raise RuntimeError(f"重复模型记录：{key[0]} / {key[1]}")
                    seen.add(key)
                by_config[config] = rows
            else:
                errors[config] = "latest 分片没有有效模型记录"
        except Exception as e:
            errors[config] = str(e)
            print(f"[ai_fetcher] Arena {config} 失败: {e}", flush=True)
            if getattr(e, "code", None) == 429:
                # 数据服务限流往往是主机级别，继续请求其它配置只会重复撞 429。
                # 已取到的配置照常展示，其余配置沿用旧缓存，下次刷新再补。
                break
    if not by_config:
        raise RuntimeError("Arena latest 数据全部不可用")
    return by_config, errors


def _arena_score(row):
    value = row.get("rating")
    return value if isinstance(value, (int, float)) else row.get("score")


def _arena_metric_label(config):
    return "IPS" if config == "agent" or config.startswith("agent_") else "Arena 分数"


def _build_arena_source(config_rows, updated_at):
    """返回 Arena 各榜缓存和按榜单配置分开的模型对比目录。"""
    rankings, boards = {}, {}
    for config, rows in config_rows.items():
        category_rows = {}
        for row in rows:
            category_rows.setdefault(str(row.get("category") or "overall"), []).append(row)
        dimensions = []
        models = {}
        published_dates = []
        for category, group in category_rows.items():
            dim_key = _slug_key(category)
            label = ARENA_CATEGORY_LABELS.get(category, category.replace("_", " ").title())
            unit = _arena_metric_label(config)
            dimensions.append({"key": dim_key, "label": label,
                               "english": category, "unit": unit})
            group.sort(key=lambda r: (
                r.get("rank") if isinstance(r.get("rank"), (int, float)) else 10**9,
                -float(_arena_score(r) or 0)))
            items = []
            for index, row in enumerate(group):
                model = str(row.get("model_name") or "--")
                org = str(row.get("organization") or "--")
                score = float(_arena_score(row))
                rank = row.get("rank")
                rank = int(rank) if isinstance(rank, (int, float)) else index + 1
                sample_count = row.get("vote_count")
                if sample_count is None:
                    sample_count = row.get("observation_count") or row.get("session_count")
                if isinstance(sample_count, (int, float)) and math.isfinite(float(sample_count)):
                    sample_count = int(sample_count)
                low = row.get("rating_lower")
                high = row.get("rating_upper")
                if low is None:
                    low = row.get("score_ci_lower")
                if high is None:
                    high = row.get("score_ci_upper")
                ci = None
                if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                    ci = f"{float(low):.1f}–{float(high):.1f}"
                published = str(row.get("leaderboard_publish_date") or "")
                if published:
                    published_dates.append(published)
                item = {
                    "rank": rank, "model": model, "org": org,
                    "score": round(score, 3), "value": round(score, 3), "unit": unit,
                    "ci": ci or "--", "votes": sample_count or 0,
                    "license": row.get("license") or "--",
                    "badge": "榜首" if rank == 1 else "",
                    "published_at": published,
                }
                if index < 100:
                    items.append(item)
                record = models.setdefault(model, {
                    "name": model, "org": org, "cap": {}, "rank_by_dimension": {},
                    "license": row.get("license") or "--",
                })
                record["cap"][dim_key] = round(score, 3)
                record["rank_by_dimension"][dim_key] = rank
                if category == "overall":
                    record["overall"] = round(score, 3)
                    record["rank"] = rank
                    record["votes"] = sample_count or 0
                    record["ci"] = ci or "--"
            board_key = f"arena__{config}__{dim_key}"
            board_label = ARENA_CONFIG_LABELS.get(config, config.replace("_", " ").title())
            rankings[board_key] = {
                "name": f"Arena · {board_label} · {label}",
                "subnav_name": f"{board_label} · {label}",
                "benchmark": f"Arena {unit} · {board_label}",
                "description": "官方最新分片；表格保留分数、置信区间、投票/观测量与发布日期。",
                "updated_at": updated_at,
                "published_at": max(published_dates) if published_dates else "",
                "source": "live", "source_id": "arena", "board": config,
                "category": category, "items": items,
            }
            if config == "text" and category == "overall":
                rankings["arena"] = rankings[board_key]

        dimensions.sort(key=lambda d: (d["key"] != "overall", d["label"]))
        board_models = sorted(models.values(),
                              key=lambda m: (m.get("rank", 10**9), m["name"].lower()))
        boards[config] = {
            "board": config,
            "board_label": ARENA_CONFIG_LABELS.get(config, config.replace("_", " ").title()),
            "updated_at": updated_at,
            "published_at": max(published_dates) if published_dates else "",
            "dimensions": dimensions,
            "models": board_models,
        }
    if not boards:
        raise RuntimeError("Arena 数据未生成可用榜单")
    return rankings, {"updated_at": updated_at, "boards": boards,
                      "board_options": [{"id": key, "name": val["board_label"]}
                                        for key, val in boards.items()]}


def _js_model_link_map(source):
    out = {}
    for match in re.finditer(r"[\"']([^\"']+)[\"']\s*:\s*\{([^{}]*)\}", source):
        model_id, body = match.groups()
        values = {}
        for field in ("displayName", "organization", "openweight", "reasoner"):
            found = re.search(rf"\b{field}\s*:\s*(?:\"([^\"]*)\"|'([^']*)'|(true|false))", body)
            if found:
                values[field] = next((g for g in found.groups() if g is not None), "")
        out[model_id] = values
    return out


def _fetch_livebench_source():
    """读取 LiveBench 最新公开 release，依照上游公式生成总分与七类分。"""
    manifest = _http_get(LIVEBENCH_CONSTANTS_URL, timeout=25).decode("utf-8", "replace")
    releases_block = re.search(r"RELEASES\s*=\s*\[(.*?)\]", manifest, re.S)
    releases = re.findall(r"\"(20\d{2}-\d{2}-\d{2})\"", releases_block.group(1)) if releases_block else []
    if not releases:
        raise RuntimeError("LiveBench release 清单缺失或格式变化")
    release = releases[-1]
    token = release.replace("-", "_")
    raw_base = f"{LIVEBENCH_REPO}/public"
    table_text = _http_get(f"{raw_base}/table_{token}.csv", timeout=30).decode("utf-8-sig", "replace")
    categories_text = _http_get(f"{raw_base}/categories_{token}.json", timeout=25).decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(table_text)))
    categories = json.loads(categories_text)
    if len(rows) < 10 or not isinstance(categories, dict) or len(categories) < 3:
        raise RuntimeError("LiveBench 数据行数或类别不足")
    links = {}
    try:
        links_text = _http_get(LIVEBENCH_MODEL_LINKS_URL, timeout=25).decode("utf-8", "replace")
        links = _js_model_link_map(links_text)
    except Exception as e:
        print(f"[ai_fetcher] LiveBench 模型信息抓取失败，使用模型 ID: {e}", flush=True)

    category_list = list(categories.items())
    labels = {"IF": "Instruction Following"}
    data = []
    for row in rows:
        model_id = str(row.get("model") or "").strip()
        if not model_id:
            continue
        cap, category_values = {}, []
        for category, tasks in category_list:
            values = []
            for task in tasks:
                try:
                    value = float(row.get(task, ""))
                    if math.isfinite(value):
                        values.append(value)
                except (TypeError, ValueError):
                    pass
            if values:
                avg = sum(values) / len(values)
                key = _slug_key(category)
                cap[key] = round(avg, 2)
                category_values.append(avg)
        if not cap:
            continue
        meta = links.get(model_id, {})
        overall = sum(category_values) / len(category_values)
        data.append({
            "id": model_id,
            "name": meta.get("displayName") or model_id,
            "org": meta.get("organization") or model_id.split("-")[0].title(),
            "open_weights": str(meta.get("openweight", "")).lower() == "true",
            "reasoning": str(meta.get("reasoner", "")).lower() == "true",
            "overall": round(overall, 2), "cap": cap,
        })
    if len(data) < 10:
        raise RuntimeError("LiveBench 有效模型不足")
    data.sort(key=lambda m: (-m["overall"], m["name"].lower()))
    updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    dimensions = [{"key": _slug_key(cat), "label": labels.get(cat, cat),
                   "english": cat, "unit": "%"} for cat, _ in category_list]
    rankings = {}
    all_scored = [(m["overall"], m) for m in data]
    all_scored.sort(key=lambda item: -item[0])
    overall_items = [{"rank": i + 1, "model": m["name"], "org": m["org"],
                      "score": m["overall"], "value": m["overall"], "unit": "%",
                      "open_weights": m["open_weights"], "reasoning": m["reasoning"],
                      "published_at": release,
                      "badge": "LiveBench 综合第一" if i == 0 else ""}
                     for i, (_, m) in enumerate(all_scored)]
    rankings["livebench__overall"] = {
        "name": "LiveBench · 综合", "benchmark": f"LiveBench {release} · 七类平均分",
        "subnav_name": "综合",
        "description": "总分按官方页面算法先算类别均分，再对有分的类别取平均。",
        "updated_at": updated_at, "published_at": release,
        "source": "live", "source_id": "livebench", "items": overall_items,
    }
    for dim in dimensions:
        scored = [(m["cap"][dim["key"]], m) for m in data if dim["key"] in m["cap"]]
        scored.sort(key=lambda item: -item[0])
        key = "livebench__" + dim["key"]
        rankings[key] = {
            "name": f"LiveBench · {dim['label']}",
            "subnav_name": dim["label"],
            "benchmark": f"LiveBench {release} · {dim['english']}",
            "description": "该类分数为有成绩子任务的算术平均值，范围 0–100。",
            "updated_at": updated_at, "published_at": release,
            "source": "live", "source_id": "livebench",
            "items": [{"rank": i + 1, "model": m["name"], "org": m["org"],
                       "score": value, "value": value, "unit": "%",
                       "open_weights": m["open_weights"], "reasoning": m["reasoning"],
                       "published_at": release,
                       "badge": "该类第一" if i == 0 else ""}
                      for i, (value, m) in enumerate(scored)],
        }
    catalog = {"updated_at": updated_at, "published_at": release,
               "dimensions": dimensions, "models": data}
    return rankings, catalog


def _fetch_datalearner_boards():
    """抓 datalearner 聚合评测榜（AA 智能指数 + LMArena Elo）。

    页面是 Next.js SSR：模型数据以 JSON 内嵌在 self.__next_f RSC 流里，
    纯 urllib 可解析、无需无头浏览器。两榜各 top 10，行字段
    rank/modelName/modelCode/score/organization[+ci/votes/license]，
    榜类型按其后紧跟的 scoreLabel 区分（"Elo" / "分数"）。
    """
    html = _http_get(DATALEARNER_URL, timeout=20).decode("utf-8", "replace")
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\((\[1,".*?\])\)</script>', html, re.S):
        try:
            parts.append(json.loads(m.group(1))[1])
        except Exception:
            pass
    stream = "".join(parts)
    dec = json.JSONDecoder()
    boards = {}
    for m in re.finditer(r'"rows":\[', stream):
        rows, end = dec.raw_decode(stream, m.end() - 1)
        label = re.search(r'"scoreLabel":"([^"]+)"', stream[end:end + 200])
        label = label.group(1) if label else ""
        if "Elo" in label:
            boards["arena"] = rows
        elif "分数" in label:
            boards["aa_index"] = rows
    if not boards:
        raise RuntimeError("页面未内嵌 rows 评测数据（可能改版）")
    return boards


def _fmt_compact(n):
    n = int(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(n)


def _fetch_existing_rankings():
    """抓 HF + OpenRouter，组装四榜。任一源失败则该榜回退内置快照。"""
    out = {}
    hf = None
    try:
        hf = json.loads(_http_get(HF_API).decode("utf-8"))
    except Exception as e:
        print(f"[ai_fetcher] HF API 失败: {e}", flush=True)
    or_models = None
    try:
        or_models = json.loads(_http_get(OPENROUTER_API).decode("utf-8"))["data"]
    except Exception as e:
        print(f"[ai_fetcher] OpenRouter API 失败: {e}", flush=True)

    today = time.strftime("%Y-%m-%d")

    # ---- 综合热度总榜（HF 全平台下载量）----
    if hf:
        # 过滤测试/实验性模型（tiny-*、test 等下载量虚高噪音）
        hf_main = [m for m in hf
                   if not re.search(r"tiny[-_.]|debug|test| playground",
                                    m["id"], re.I)]
        items = []
        for i, m in enumerate(hf_main[:100]):
            lic = next((t.split(":", 1)[1] for t in m.get("tags", [])
                        if t.startswith("license:")), "自定义")
            items.append({
                "rank": i + 1, "model": m["id"], "org": m.get("author", "--"),
                "downloads": _fmt_compact(m.get("downloads")),
                "likes": str(m.get("likes", 0)),
                "updated": (m.get("lastModified") or "")[:10],
                "license": lic,
                "badge": "热度第一" if i == 0 else ("开源顶流" if i < 3 else ""),
            })
        out["overall"] = {
            "name": "开源模型热度总榜", "benchmark": "HuggingFace 下载量 / 点赞",
            "description": "HF 全平台真实下载量排序（仅覆盖开源模型）。闭源旗舰（Claude/GPT/Gemini 等）不在 HF 记账，全模型能力口径见 AA 智能指数榜与 LMArena 偏好榜。",
            "updated_at": today, "items": items, "source": "live",
        }

    # ---- 开源生态榜（HF：非专有许可）----
    if hf:
        items, rank = [], 0
        for m in hf:
            tags = m.get("tags", [])
            lic = next((t.split(":", 1)[1] for t in tags
                        if t.startswith("license:")), "")
            if not lic or lic in ("other", "custom"):
                continue
            rank += 1
            items.append({
                "rank": rank, "model": m["id"], "org": m.get("author", "--"),
                "params": next((t for t in tags if "size" in t or "B" in t
                                and "params" not in t), "-"),
                "license": lic,
                "downloads": _fmt_compact(m.get("downloads")),
                "likes": str(m.get("likes", 0)),
                "badge": "开源热度第一" if rank == 1 else "",
            })
            if rank >= 50:
                break
        if items:
            out["opensource"] = {
                "name": "开源模型生态热度榜", "benchmark": "HuggingFace 下载量（开放许可）",
                "description": "仅收录开放许可模型，按真实下载量排序——热度即生态。",
                "updated_at": today, "items": items, "source": "live",
            }

    # ---- 代码编程榜（HF：代码模型热度）----
    if hf:
        items, rank = [], 0
        for m in hf:
            ident = (m["id"] + " " + " ".join(m.get("tags", []))).lower()
            if not any(k in ident for k in ("coder", "code", "codestral", "devstral")):
                continue
            rank += 1
            items.append({
                "rank": rank, "model": m["id"], "org": m.get("author", "--"),
                "downloads": _fmt_compact(m.get("downloads")),
                "likes": str(m.get("likes", 0)),
                "updated": (m.get("lastModified") or "")[:10],
                "badge": "代码模型热度第一" if rank == 1 else "",
            })
            if rank >= 50:
                break
        if items:
            out["coding"] = {
                "name": "代码模型生态热度榜", "benchmark": "HuggingFace 下载量（代码特化模型）",
                "description": "全部代码特化开源模型按真实下载量排序。SWE-bench 等跑分榜无公开 API，暂以生态热度口径替代。",
                "updated_at": today, "items": items, "source": "live",
            }

    # ---- 性价比与上下文榜（OpenRouter 真实价格）----
    if or_models:
        priced = []
        for m in or_models:
            p = m.get("pricing") or {}
            try:
                out_price = float(p.get("completion") or 1) * 1e6
                in_price = float(p.get("prompt") or 1) * 1e6
            except Exception:
                continue
            if out_price <= 0 and in_price <= 0:
                continue                      # 排除无定价/免费占位
            priced.append((m, in_price, out_price))
        priced.sort(key=lambda x: x[2])
        items = []
        for i, (m, ip, op) in enumerate(priced[:100]):
            items.append({
                "rank": i + 1, "model": m["id"], "org": (m["id"].split("/")[0]),
                "input_cost": f"${ip:.2f}" if ip >= 0.01 else f"${ip:.3f}",
                "output_cost": f"${op:.2f}" if op >= 0.01 else f"${op:.3f}",
                "context": _fmt_compact(m.get("context_length") or 0) + " tok",
                "badge": "全网最低价" if i == 0 else "",
            })
        out["efficiency"] = {
            "name": "性价比与上下文窗口榜", "benchmark": "OpenRouter 官方定价（USD / 1M Tokens）",
            "description": "447 个接入模型的官方真实定价，按每百万 Token 输出价格升序取前 100。吞吐速度无公开统一源，暂不展示。",
            "updated_at": today, "items": items, "source": "live",
        }

    # ---- 能力评测两榜（官方源直取 top100，datalearner 聚合页兜底）----
    # AA 官方榜偶发 SSL 断流（实测：一次失败、紧接着一次就成功），所以单独给它
    # 2 次重试；其余源不加——偶发失败率不足以抵消拖长整体抓取时间的代价。
    aa_raw = None
    for attempt in range(3):
        try:
            aa_raw = _fetch_aa_rows()
            break
        except Exception as e:
            if attempt == 2:
                print(f"[ai_fetcher] AA 官方榜失败: {e}", flush=True)
            else:
                time.sleep(2 + attempt)        # 间隔 2s、3s
    aa_rows = None
    if aa_raw is not None:
        try:
            aa_rows = _fetch_aa_index(aa_raw)      # 复用同一份抓取
        except Exception as e:
            print(f"[ai_fetcher] AA 官方榜失败: {e}", flush=True)
    arena_rows = None
    try:
        arena_rows = _fetch_arena_direct()
    except Exception as e:
        print(f"[ai_fetcher] LMArena 官方榜失败: {e}", flush=True)
    if aa_rows is None or arena_rows is None:
        try:
            dl = _fetch_datalearner_boards()
        except Exception as e:
            print(f"[ai_fetcher] datalearner 抓取失败: {e}", flush=True)
            dl = {}
        if aa_rows is None and "aa_index" in dl:
            aa_rows = [{
                "rank": r.get("rank"), "model": r.get("modelName"),
                "org": r.get("organization") or "--",
                "score": r.get("score"),
                "thinking": "深度思考" if r.get("thinkingMode") else "-",
                "badge": "综合能力第一" if r.get("rank") == 1 else "",
            } for r in dl["aa_index"][:10]]
        if arena_rows is None and "arena" in dl:
            arena_rows = [{
                "rank": r.get("rank"), "model": r.get("modelName"),
                "org": r.get("organization") or "--",
                "elo": r.get("score"), "ci": r.get("ci") or "-",
                "votes": _fmt_compact(r.get("votes")),
                "license": r.get("license") or "-",
                "badge": "人类偏好第一" if r.get("rank") == 1 else "",
            } for r in dl["arena"][:10]]
    if aa_rows:
        out["aa_index"] = {
            "name": "AA 智能指数榜", "benchmark": "Artificial Analysis 综合评测（官方直取）",
            "description": "第三方评测机构对知识/推理/数学等多维基准的加权综合分，反映模型硬能力（取前 100）。",
            "updated_at": today, "items": aa_rows, "source": "live",
        }
    if arena_rows:
        out["arena"] = {
            "name": "LMArena 人类偏好榜", "benchmark": "LMArena 盲测 Elo（官方直取）",
            "description": "大社区盲测投票 Elo，官方页面内嵌全表直取前 100。",
            "updated_at": today, "items": arena_rows, "source": "live",
        }

    # ---- 八维能力榜 + 模型对比目录（与智能指数榜同一份 AA 原始行）----
    if aa_raw:
        out.update(_capability_boards(aa_raw, today))
    catalog = build_model_catalog(aa_raw, today) if aa_raw else {}

    return out, catalog


def _fetch_aa_source():
    today = time.strftime("%Y-%m-%d")
    rows = _fetch_aa_rows()
    rankings = {"aa_index": {
        "name": "AA 智能指数榜", "benchmark": "Artificial Analysis 综合评测（官方直取）",
        "description": "第三方评测机构对知识、推理、数学等基准的加权综合分。",
        "updated_at": today, "items": _fetch_aa_index(rows), "source": "live",
    }}
    rankings.update(_capability_boards(rows, today))
    return rankings, build_model_catalog(rows, today)


def _arena_direct_catalog(items, updated_at):
    models, dimensions = [], [{"key": "overall", "label": "Text Arena 综合",
                               "english": "Text Arena", "unit": "Arena rating"}]
    for item in items:
        value = item.get("elo")
        if not isinstance(value, (int, float)):
            continue
        models.append({"name": item.get("model") or "--", "org": item.get("org") or "--",
                       "overall": value, "rank": item.get("rank"),
                       "ci": item.get("ci") or "--", "license": item.get("license") or "--",
                       "votes_label": item.get("votes") or "--",
                       "cap": {"overall": value}})
    return {"updated_at": updated_at, "published_at": "",
            "boards": {"text": {"board": "text", "board_label": "文本",
                                "updated_at": updated_at,
                                "published_at": "",
                                "dimensions": dimensions, "models": models}},
            "board_options": [{"id": "text", "name": "文本"}]}


def _append_arena_text_fallback(rankings, catalog, items, updated_at):
    """把已验证可用的 Arena 文本榜放入相同目录结构，供榜单与对比共同回退。"""
    fallback = _arena_direct_catalog(items, updated_at)
    if not isinstance(catalog, dict):
        catalog = {"updated_at": updated_at, "boards": {}, "board_options": []}
    boards = dict(catalog.get("boards") or {})
    boards["text"] = fallback["boards"]["text"]
    catalog["boards"] = boards
    options = {item.get("id"): item for item in (catalog.get("board_options") or [])}
    options["text"] = {"id": "text", "name": "文本"}
    catalog["board_options"] = list(options.values())

    items_out = [{
        "rank": item.get("rank"), "model": item.get("model"),
        "org": item.get("org"), "score": item.get("elo"),
        "value": item.get("elo"), "unit": "Arena 分数",
        "ci": item.get("ci") or "--", "votes": item.get("votes") or "--",
        "license": item.get("license") or "--", "badge": item.get("badge") or "",
    } for item in items]
    ranking = {
        "name": "Arena · 文本 · 综合",
        "subnav_name": "文本 · 综合",
        "benchmark": "Arena 官方 Text Arena rating（网页回退）",
        "description": "数据集 API 限流时回退到 Arena 官方文本榜；发布日期未提供。",
        "updated_at": updated_at, "source": "live", "source_id": "arena",
        "board": "text", "category": "overall", "items": items_out,
    }
    rankings["arena__text__overall"] = ranking
    rankings["arena"] = ranking
    return rankings, catalog


def _fetch_media_rankings():
    """抓媒体细分榜（视频 / 生图 / 音频）。

    模型资料单独落在 `MEDIA_METADATA_CACHE`，**不写进对比目录缓存**
    （`ai_models_cache.json`）。单榜失败只报错并把旧缓存留着，由
    `_merge_rankings_cache` 的「空 items 不覆盖」规则兜底。
    """
    old_meta = _load_json(MEDIA_METADATA_CACHE, {})
    if not isinstance(old_meta, dict):
        old_meta = {}
    rankings, new_meta, errors = ai_media_rankings.fetch_media_rankings(
        _http_get, _rsc_stream, _dataset_api_json, old_meta)
    if isinstance(new_meta, dict) and new_meta != old_meta:
        try:
            with open(MEDIA_METADATA_CACHE, "w", encoding="utf-8") as f:
                json.dump(new_meta, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"[ai_fetcher] 媒体模型资料缓存写入失败: {e}", flush=True)
    return rankings, errors


def fetch_rankings(source=None):
    """抓所有榜源，或只抓指定数据库及其对应榜单和对比目录。"""
    allowed = {None, "aa", "arena", "livebench"}
    if source not in allowed:
        raise ValueError("未知模型数据源: " + str(source))
    if source == "aa":
        rankings, catalog = _fetch_aa_source()
        return rankings, {"aa": catalog}, {}

    if source == "arena":
        updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            config_rows, errors = _fetch_arena_dataset_rows()
            rankings, catalog = _build_arena_source(config_rows, updated_at)
            if "text" not in config_rows:
                try:
                    items = _fetch_arena_direct()
                    rankings, catalog = _append_arena_text_fallback(
                        rankings, catalog, items, updated_at)
                    errors.pop("text", None)
                except Exception as fallback_error:
                    errors["text_fallback"] = str(fallback_error)
            return rankings, {"arena": catalog}, errors
        except Exception as e:
            print(f"[ai_fetcher] Arena 数据集失败，尝试旧文本榜回退: {e}", flush=True)
            items = _fetch_arena_direct()
            return ({"arena": {
                "name": "LMArena 人类偏好榜",
                "benchmark": "Arena 官方 Text Arena rating",
                "description": "Hugging Face 数据集不可用时回退到 Arena 官方文本榜。",
                "updated_at": updated_at, "items": items, "source": "live",
            }}, {"arena": _arena_direct_catalog(items, updated_at)},
                    {"arena": str(e)})

    if source == "livebench":
        rankings, catalog = _fetch_livebench_source()
        return rankings, {"livebench": catalog}, {}

    # 全量更新继续保留已有 HF、OpenRouter、AA 与 LMArena 抓取逻辑。
    rankings, aa_catalog = _fetch_existing_rankings()
    catalogs = {"aa": aa_catalog} if aa_catalog.get("models") else {}
    errors = {}
    updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        config_rows, arena_errors = _fetch_arena_dataset_rows()
        arena_rankings, arena_catalog = _build_arena_source(config_rows, updated_at)
        if "text" not in config_rows:
            try:
                items = rankings.get("arena", {}).get("items") or _fetch_arena_direct()
                arena_rankings, arena_catalog = _append_arena_text_fallback(
                    arena_rankings, arena_catalog, items, updated_at)
                arena_errors.pop("text", None)
            except Exception as fallback_error:
                arena_errors["text_fallback"] = str(fallback_error)
        rankings.update(arena_rankings)
        catalogs["arena"] = arena_catalog
        if arena_errors:
            errors["arena"] = "; ".join(f"{k}: {v}" for k, v in arena_errors.items())
    except Exception as e:
        errors["arena"] = str(e)
        print(f"[ai_fetcher] Arena 分类榜失败，保留已有文本榜缓存: {e}", flush=True)
        try:
            items = rankings.get("arena", {}).get("items") or _fetch_arena_direct()
            if items:
                catalogs["arena"] = _arena_direct_catalog(items, updated_at)
        except Exception as fallback_error:
            errors["arena"] += "; text fallback: " + str(fallback_error)
    try:
        livebench_rankings, livebench_catalog = _fetch_livebench_source()
        rankings.update(livebench_rankings)
        catalogs["livebench"] = livebench_catalog
    except Exception as e:
        errors["livebench"] = str(e)
        print(f"[ai_fetcher] LiveBench 失败（保留旧缓存）: {e}", flush=True)
    # 媒体细分榜只在全量刷新里抓；source 指定单一对比库时不碰它们。
    try:
        media_rankings, media_errors = _fetch_media_rankings()
        rankings.update(media_rankings)
        errors.update({"media:" + board_id: message
                       for board_id, message in (media_errors or {}).items()})
    except Exception as e:
        errors["media"] = str(e)
        print(f"[ai_fetcher] 媒体榜单失败（保留旧缓存）: {e}", flush=True)
    if not aa_catalog.get("models"):
        errors["aa"] = "AA 模型目录未抓到新数据"
    return rankings, catalogs, errors


NEWS_IMAGE_HOSTS = (
    "twimg.com",                 # X 帖子图（pbs.twimg.com）
    "huggingface.co",            # HF 论文封面 cdn-thumbnails / cdn-uploads
    "techcrunch.com",
    "theverge.com", "vox-cdn.com",
    "jiqizhixin.com",            # 机器之心
    "qbitai.com",                # 量子位
    "the-decoder.com",
    "ithome.com",
    "wp.com",                    # WordPress 图床（i0.wp.com 等，TC/Verge 常见）
)

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# og:image 抓到的图床域名不可预知（实测 substackcdn.com / sierra.ai / pbs.twimg.com
# …随源站走），静态白名单会一直拦掉合法配图。抓到图时把其域名登记进这个运行时
# 集合并随缓存持久化（缓存顶层 image_hosts）；只登记公网域名，IP 字面量与内网
# 地址一律不登记——这个接口等于是内网可用的图片代理，不能被第三方 og:image
# 当成内网探测跳板。
_DYN_IMG_HOSTS = set()
_DYN_IMG_LOCK = threading.Lock()
_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|\d{1,3}(?:\.\d{1,3}){3}|.*\.local)$")


def _register_image_host(url):
    try:
        h = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return
    if not h or "." not in h or _PRIVATE_HOST_RE.match(h):
        return
    with _DYN_IMG_LOCK:
        _DYN_IMG_HOSTS.add(h)


def _load_dyn_image_hosts(path):
    """启动/刷新时从缓存顶层预载上一次登记的图床域名（重启后白名单不丢）。"""
    for h in (_load_json(path, {}).get("image_hosts") or []):
        if isinstance(h, str) and "." in h and not _PRIVATE_HOST_RE.match(h):
            _DYN_IMG_HOSTS.add(h.lower())


def _save_dyn_image_hosts():
    with _DYN_IMG_LOCK:
        return sorted(_DYN_IMG_HOSTS)


def fetch_remote_image(src):
    """新闻/要闻帖子配图统一经本机 8790 转发，返回 (bytes, 扩展名)。

    为什么要由后端转一手（stats_server `GET /api/ai/image`）：部分站点给静态
    资源带 `Cross-Origin-Resource-Policy: same-origin`，Chromium 内核会**直接
    拒发**跨源请求——`<img>` 与 `fetch` 都是即时 Failed to fetch，而 curl/urllib
    不受 CORP 约束，所以命令行怎么测都正常，只有浏览器端图片全不显示（桌面端
    WebView2 与 headless Chrome 实测一致，2026-09-21）。同源地由后端取回再发出
    即绕开 CORP，顺带也让前端不直连第三方、统一吃 86400s 浏览器缓存。

    域名按 NEWS_IMAGE_HOSTS 后缀匹配 + og:image 抓取时动态登记的图床域名放行；
    单图上限 8MB 防异常大图占内存。扩展名优先取路径，取不到（twimg
    ?format=webp 这类）回退 Content-Type。
    """
    u = urlparse(src)
    host = (u.netloc or "").lower()
    if u.scheme != "https" or ".." in u.path:
        raise ValueError("不支持的图片地址")
    host_ok = any(host == h or host.endswith("." + h) for h in NEWS_IMAGE_HOSTS)
    if not host_ok:
        bare = host.split("@")[-1].split(":")[0]
        with _DYN_IMG_LOCK:
            host_ok = bare in _DYN_IMG_HOSTS
    if not host_ok:
        raise ValueError("不支持的图片地址")
    req = urlreq.Request(src, headers={"User-Agent": UA, "Accept": "image/*"})
    op = urlreq.build_opener(urlreq.ProxyHandler(urlreq.getproxies()))
    with op.open(req, timeout=30) as r:
        data = r.read(8 * 1024 * 1024)
        ctype = (r.headers.get("Content-Type") or "").lower()
    ext = os.path.splitext(u.path)[1].lower()
    if ext not in _IMG_EXTS:
        sub = ctype.split("/")[-1].split(";")[0].strip()
        ext = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
               "webp": ".webp", "gif": ".gif"}.get(sub, ".png")
    return data, ext


# ================================================== 缓存与调度
def _load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _cache_age_hours(path):
    try:
        return (time.time() - path.stat().st_mtime) / 3600
    except Exception:
        return 1e9


def refresh_news():
    """抓新闻写缓存，返回 (条数, 成功源数)。失败保留旧缓存。"""
    _load_dyn_image_hosts(NEWS_CACHE)    # 重启后恢复 og:image 图床白名单
    try:
        items, ok = fetch_news()
        if items and ok >= 2:
            with open(NEWS_CACHE, "w", encoding="utf-8") as f:
                json.dump({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "image_hosts": _save_dyn_image_hosts(),
                           "items": items}, f, ensure_ascii=False, indent=1)
            print(f"[ai_fetcher] 新闻缓存已更新: {len(items)} 条 / {ok} 源",
                  flush=True)
            return len(items), ok
        print(f"[ai_fetcher] 新闻抓取结果不足（{len(items)} 条 / {ok} 源），"
              "保留旧缓存", flush=True)
        return 0, ok
    except Exception as e:
        print(f"[ai_fetcher] 新闻刷新异常: {e}", flush=True)
        return 0, 0


def refresh_briefings():
    """抓第三方每日要闻写独立缓存，返回条数。

    """
    items, provider, source_url = None, "", ""
    if not items:
        print("[ai_fetcher] 每日要闻各源全失败（保留旧缓存）", flush=True)
        return 0
    # 帖子化配图：最新两期逐节抓「阅读原文」og:image（失败留空，不阻塞发刊）
    _load_dyn_image_hosts(BRIEF_CACHE)   # 重启后恢复 og:image 图床白名单
    try:
        _enrich_briefing_images(items)
    except Exception as e:
        print(f"[ai_fetcher] 要闻帖子配图抓取异常（不影响发刊）: {e}", flush=True)
    with open(BRIEF_CACHE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "provider": provider,
                   "source_url": source_url,
                   "image_hosts": _save_dyn_image_hosts(),
                   "items": items}, f, ensure_ascii=False, indent=1)
    print(f"[ai_fetcher] 每日要闻已更新（{provider}）: {len(items)} 期", flush=True)
    return len(items)


def _merge_rankings_cache(new_data):
    """把本轮抓到的榜单并入旧缓存，而不是整体覆盖。

    单次抓取常有某个源失败（实测：AA 官方榜偶发 SSL 断流，8 个 cap_* 八维能力榜
    会整批缺席），整体覆盖写会让这些分类从缓存里消失、页面显示「缓存缺失」——
    旧数据仍有价值，所以只覆盖 items 非空的分类，本轮没抓到的分类保留旧值。
    顶层 updated_at 照写，语义是「最近一次抓取尝试时间」。
    返回合并后的 keys 列表（供日志用）。
    """
    old = {}
    try:
        with open(RANK_CACHE, "r", encoding="utf-8") as f:
            old = (json.load(f) or {}).get("rankings") or {}
    except Exception:
        old = {}                               # 首次运行或缓存损坏：按空处理
    merged = dict(old)
    for key, val in (new_data or {}).items():
        # 只认「有内容」的分类：空 items 是抓取失败的产物，不能拿它盖掉旧数据
        if isinstance(val, dict) and val.get("items"):
            merged[key] = val
    with open(RANK_CACHE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "rankings": merged}, f, ensure_ascii=False, indent=1)
    return sorted(merged)


def _merge_model_catalog_cache(new_catalogs):
    cached = _load_json(MODELS_CACHE, {})
    if not isinstance(cached, dict):
        cached = {}
    sources = cached.get("sources")
    if not isinstance(sources, dict):
        # 旧版 ai_models_cache.json 是一个 AA 目录，迁移时保持可读。
        sources = {"aa": cached} if isinstance(cached, dict) and cached.get("models") else {}
    else:
        sources = dict(sources)

    for source, catalog in (new_catalogs or {}).items():
        if not isinstance(catalog, dict):
            continue
        if source == "arena" and isinstance(catalog.get("boards"), dict):
            old_arena = sources.get("arena") or {}
            boards = dict(old_arena.get("boards") or {})
            boards.update(catalog["boards"])
            catalog = dict(catalog)
            catalog["boards"] = boards
            # 本轮失败的配置沿用旧值；选项按合并后的实际目录生成。
            catalog["board_options"] = [
                {"id": key, "name": value.get("board_label") or key}
                for key, value in boards.items()
            ]
        valid = (bool(catalog.get("models"))
                 or any(bool(v.get("models")) for v in (catalog.get("boards") or {}).values()))
        if valid:
            sources[source] = catalog

    output = {"schema_version": 2, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "sources": sources}
    with open(MODELS_CACHE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    return sorted(sources)


def refresh_rankings(source=None):
    try:
        data, catalogs, errors = fetch_rankings(source)
        if data:
            print(f"[ai_fetcher] 排行榜缓存已更新: {_merge_rankings_cache(data)}",
                  flush=True)
        if catalogs:
            sources = _merge_model_catalog_cache(catalogs)
            print(f"[ai_fetcher] 模型对比来源已更新: {sources}", flush=True)
        return {"source": source or "all", "updated": sorted(data), "errors": errors}
    except Exception as e:
        print(f"[ai_fetcher] 排行榜刷新异常: {e}", flush=True)
        return {"source": source or "all", "updated": [], "errors": {source or "all": str(e)}}


def refresh_all():
    refresh_news()
    refresh_briefings()
    refresh_rankings()


# -------------------------------------------------- 天梯手动刷新（异步任务状态）
# 抓取要跑 1-3 分钟，接口必须立即返回，所以把状态放模块级、由前端按 finished_at
# 的变化判断「这一轮」何时结束（只看 running 会误认成上一轮的结果）。
_rank_refresh = {"running": False, "started_at": None, "finished_at": None,
                 "result": None, "error": None, "source": None}
_rank_refresh_lock = threading.Lock()


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_refresh_rankings_async(source=None):
    """后台刷新全部榜源或单个数据库；已有任务在跑则不重复启动。"""
    if source not in (None, "aa", "arena", "livebench"):
        raise ValueError("未知模型数据源: " + str(source))
    with _rank_refresh_lock:
        if _rank_refresh["running"]:
            return False
        _rank_refresh.update(running=True, started_at=_now_str(),
                             finished_at=None, result=None, error=None,
                             source=source or "all")

    def _work():
        # refresh_rankings 自身已吞异常并返回 []，这里的兜底只覆盖它之外的意外
        result, error = None, None
        try:
            result = refresh_rankings(source)
            source_errors = result.get("errors") if isinstance(result, dict) else None
            if source_errors:
                detail = "; ".join(f"{key}: {value}" for key, value in source_errors.items())
                error = detail if len(detail) <= 600 else detail[:597] + "..."
        except Exception as e:                     # noqa: BLE001
            error = str(e)
        finally:
            with _rank_refresh_lock:
                _rank_refresh.update(running=False, finished_at=_now_str(),
                                     result=result, error=error)

    threading.Thread(target=_work, daemon=True).start()
    return True


def rankings_refresh_status(source=None):
    """任务状态 + 缓存里的 updated_at（前端据此判断这轮刷新是否真的落了盘）。"""
    with _rank_refresh_lock:
        st = dict(_rank_refresh)
    st["cache_updated_at"] = _load_json(RANK_CACHE, {}).get("updated_at")
    if source:
        model_cache = _load_json(MODELS_CACHE, {})
        if not isinstance(model_cache, dict):
            model_cache = {}
        sources = model_cache.get("sources") or {}
        source_data = sources.get(source) or {}
        if source == "arena" and source_data.get("boards"):
            board = source_data["boards"].get("text") or next(iter(source_data["boards"].values()))
            st["source_cache_updated_at"] = board.get("updated_at") or source_data.get("updated_at")
            st["published_at"] = board.get("published_at") or source_data.get("published_at") or ""
        else:
            st["source_cache_updated_at"] = source_data.get("updated_at") or ""
            st["published_at"] = source_data.get("published_at") or ""
    return st


_sched_stop = threading.Event()
_sched_started = False


def _scheduler_loop():
    """每天 19:00 抓取；启动时缓存过期立即补一次；19 点后每小时补偿重试。"""
    # 启动补偿（三个缓存任一过期即整批补抓）
    if max(_cache_age_hours(NEWS_CACHE), _cache_age_hours(RANK_CACHE),
           _cache_age_hours(BRIEF_CACHE)) > STALE_HOURS:
        refresh_all()
    done_day = None
    while not _sched_stop.wait(600):
        now = time.localtime()
        if now.tm_hour == FETCH_HOUR and done_day != now.tm_yday:
            refresh_all()
            done_day = now.tm_yday
        elif now.tm_hour > FETCH_HOUR and done_day is None:
            # 进程在 19 点后启动且当天从未抓过（缓存仍是昨天的）→ 补一次
            if _cache_age_hours(NEWS_CACHE) > 20:
                refresh_all()
                done_day = now.tm_yday
    print("[ai_fetcher] 定时调度已退出（AI 模块全部卸载）", flush=True)


def start_scheduler():
    """启动定时调度（幂等）。由 stats_server.start_in_thread() / 模块安装拉起。"""
    global _sched_started
    if _sched_started:
        return
    _sched_started = True
    _sched_stop.clear()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print("[ai_fetcher] 新闻/要闻/排行榜定时调度已启动（每日 19:00）", flush=True)


def stop_scheduler():
    """停止定时调度（AI 专区四模块全部卸载时调用）。"""
    global _sched_started
    if not _sched_started:
        return
    _sched_started = False
    _sched_stop.set()


if __name__ == "__main__":
    n, ok = refresh_news()
    print(f"新闻: {n} 条 / {ok} 源成功")
    print("每日要闻:", refresh_briefings())
    print("排行榜:", refresh_rankings())
