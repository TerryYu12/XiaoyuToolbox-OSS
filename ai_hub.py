# -*- coding: utf-8 -*-
"""小宇工具箱 · AI 专区核心数据服务 (ai_hub.py)

提供：
1. 每日 AI 新闻 (Daily AI News) - 支持多维度标签分类、搜索、离线安全缓存与在线更新。
2. 实时 AI 模型排行榜 (AI Leaderboard) - 包含 AA、Arena、LiveBench、Hugging Face 与 OpenRouter 数据。
"""
import json
import os
import sys
import time
from pathlib import Path

import ai_media_rankings

# onefile exe 里 __file__ 指向 _MEIPASS 临时解压目录，缓存读写必须落
# exe 同目录（与 ai_fetcher/stats_api 同一约定）
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
NEWS_CACHE_FILE = BASE_DIR / "ai_news_cache.json"
BRIEF_CACHE_FILE = BASE_DIR / "ai_briefings_cache.json"   # 第三方每日要闻

# ================================================== 1. 每日 AI 新闻
DEFAULT_NEWS_ITEMS = [
    {
        "id": "news-20260918-01",
        "title": "Anthropic 推出 Claude 3.7 Sonnet 混合推理模型与 Claude Code 生产级 CLI",
        "summary": "Anthropic 正式发布业界首个集标准生成与扩展思维链于一体的混合推理模型 Claude 3.7 Sonnet。同时其研发的高效终端智能体工具 Claude Code 凭借极低延迟与深度上下文整合，成为全网开发者最受瞩目的 Agent 级命令行生产力工具。",
        "category": "dev",
        "category_name": "编程 Agent",
        "source": "Anthropic Official",
        "source_url": "https://www.anthropic.com/news",
        "published_at": "2026-09-18 10:30",
        "tags": ["Claude 3.7", "Claude Code", "Agent", "混合推理"],
        "hot": True,
        "views": "18.9k"
    },
    {
        "id": "news-20260918-02",
        "title": "DeepSeek 全面开放开源生态：R1 与 V4 系列模型高并发推理成本突破新低",
        "summary": "DeepSeek 团队持续推进高性价比大模型普惠，V4 Flash 与 V3 系列模型以极低的 Token 成本和优秀的编码能力在开发者生态中迅速扩张。国内外各类反代、聚合路由网关已实现秒级平替与海量并发分发。",
        "category": "models",
        "category_name": "模型发布",
        "source": "DeepSeek Hub",
        "source_url": "https://deepseek.com",
        "published_at": "2026-09-18 09:15",
        "tags": ["DeepSeek-V4", "开源", "低成本推理", "API"],
        "hot": True,
        "views": "24.5k"
    },
    {
        "id": "news-20260917-03",
        "title": "OpenAI 启动下一代 Codex 生态重构：推出统一计算机控制协议与全平台桌面集成",
        "summary": "OpenAI 宣布升级 Codex CLI 与桌面客户端生态架构，引入深度的原生沙盒与统一计算机使用（Unified Computer Use）能力。支持与第三方本地代理无缝兼容，大幅提升复杂代码库重构能力。",
        "category": "dev",
        "category_name": "编程 Agent",
        "source": "OpenAI Engineering",
        "source_url": "https://openai.com/blog",
        "published_at": "2026-09-17 18:00",
        "tags": ["Codex", "OpenAI", "Computer Use", "CLI"],
        "hot": False,
        "views": "15.2k"
    },
    {
        "id": "news-20260917-04",
        "title": "Google 发布 Gemini 2.0 系列旗舰模型：原生多模态实时流式交互全面落地",
        "summary": "Gemini 2.0 Flash 与 Flash-Lite 展现出惊人的端到端实时流式能力。通过 Live API 突破音频与视觉协同瓶颈，并在长上下文召回率（2M Window）保持 99.8% 的极高精确度。",
        "category": "models",
        "category_name": "模型发布",
        "source": "Google DeepMind",
        "source_url": "https://deepmind.google/technologies/gemini/",
        "published_at": "2026-09-17 14:20",
        "tags": ["Gemini 2.0", "多模态", "实时流式", "Long Context"],
        "hot": True,
        "views": "19.8k"
    },
    {
        "id": "news-20260916-05",
        "title": "前沿研究：测试阶段计算缩放（Test-time Compute Scaling）与自博弈验证",
        "summary": "学术界顶会近期关于长思维链推演的多篇综述揭示，在测试阶段给予模型动态推理预算可使模型在复杂数学定理证明与算法竞赛中跨越两个数量级的性能层级，重新定义了大模型 Scaling Law。",
        "category": "research",
        "category_name": "前沿论文",
        "source": "arXiv AI Research",
        "source_url": "https://arxiv.org/abs/cs.AI",
        "published_at": "2026-09-16 21:40",
        "tags": ["Thinking Models", "Test-time Compute", "arXiv", "RL"],
        "hot": False,
        "views": "11.3k"
    },
    {
        "id": "news-20260916-06",
        "title": "NVIDIA Blackwell 架构集群在主流云厂商全速部署，AI 算力成本预期下探 30%",
        "summary": "全球主要云数据中心开始规模化上线 B200 / GB200 NVL72 算力实例，针对大参数 MoE 模型的 FP4 量化推理吞吐提升 3.8 倍，将显著降低下一代千亿级多模态模型的商用落地门槛。",
        "category": "industry",
        "category_name": "行业动态",
        "source": "TechCrunch AI",
        "source_url": "https://techcrunch.com/category/artificial-intelligence/",
        "published_at": "2026-09-16 16:50",
        "tags": ["Blackwell", "NVIDIA", "算力基础设施", "MoE"],
        "hot": False,
        "views": "13.6k"
    },
    {
        "id": "news-20260915-07",
        "title": "开源大模型 Qwen 2.5 / 3 系列全尺寸矩阵发布，开源代码与数学表现登顶",
        "summary": "阿里千问团队持续发布 Qwen 系列全尺寸架构，从 0.5B 到 72B 及旗舰 Coder 32B，在本地部署、单卡运行及生产环境服务中获得极高评价，广泛适配 GGUF 与各类量化引擎。",
        "category": "models",
        "category_name": "模型发布",
        "source": "Qwen Team",
        "source_url": "https://qwenlm.github.io/",
        "published_at": "2026-09-15 11:10",
        "tags": ["Qwen", "GGUF", "开源生态", "Local LLM"],
        "hot": False,
        "views": "16.4k"
    }
]


def load_ai_news(category="all", search=""):
    """加载 AI 新闻列表，支持标签分类与文本搜索。

    数据来源（2026-09-19 起）：ai_fetcher.py 定时抓取的真实 RSS 缓存
    （ai_news_cache.json 为 {"updated_at": ..., "items": [...]}）；
    缓存缺失时回退内置热点快照 DEFAULT_NEWS_ITEMS。
    """
    news_list = DEFAULT_NEWS_ITEMS
    updated_at = ""
    if NEWS_CACHE_FILE.exists():
        try:
            with open(NEWS_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("items"):
                news_list = cached["items"]
                updated_at = cached.get("updated_at", "")
            elif isinstance(cached, list) and cached:
                news_list = cached
        except Exception:
            pass

    # 过滤分类
    if category and category != "all":
        news_list = [n for n in news_list if n.get("category") == category]

    # 搜索过滤
    if search:
        s = search.lower().strip()
        news_list = [
            n for n in news_list
            if s in n.get("title", "").lower()
            or s in n.get("summary", "").lower()
            or any(s in t.lower() for t in n.get("tags", []))
        ]

    return {
        "status": "ok",
        "total": len(news_list),
        "updated_at": updated_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        "category": category,
        "items": news_list,
    }


# ================================================== 1b. 每日要闻（第三方源）
def load_ai_briefings(limit=30):
    """读取第三方每日要闻缓存（ai_briefings_cache.json）。

    与上面自建 RSS 快讯流相互独立：这份缓存缺失（对方改版 / 首次启动还没抓）
    时返回空列表 + source="empty"，前端只是不显示要闻区，快讯流照常。
    """
    if not BRIEF_CACHE_FILE.exists():
        return {"status": "ok", "source": "empty", "total": 0,
                "updated_at": "", "items": []}
    try:
        with open(BRIEF_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return {"status": "ok", "source": "empty", "total": 0,
                "updated_at": "", "items": []}
    items = cached.get("items") or []
    items = sorted(items, key=lambda x: x.get("published_at") or "", reverse=True)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 30
    return {
        "status": "ok",
        "source": "live",
        "provider": cached.get("provider", "llmpk"),
        "total": len(items),
        "updated_at": cached.get("updated_at", ""),
        "source_url": cached.get("source_url", ""),
        "items": items[:limit],
    }


# ================================================== 2. 实时 AI 模型排行榜
RANKINGS_DATA = {
    "overall": {
        "name": "全球大模型综合竞技天梯榜",
        "benchmark": "LMSYS Chatbot Arena / Win Rate ELO",
        "description": "基于盲测真实用户对抗评测，反映大语言模型在通识、推理与指令遵从上的综合综合实力。",
        "updated_at": "2026-09-18",
        "items": [
            {"rank": 1, "model": "Claude 3.7 Sonnet (Thinking)", "org": "Anthropic", "elo": 1368, "win_rate": "69.4%", "context": "200K / 1M", "license": "Proprietary", "badge": "天梯第一"},
            {"rank": 2, "model": "DeepSeek-R1 (671B MoE)", "org": "DeepSeek", "elo": 1352, "win_rate": "67.8%", "context": "128K", "license": "MIT (Open)", "badge": "开源推理王"},
            {"rank": 3, "model": "Gemini 2.0 Pro Experimental", "org": "Google", "elo": 1346, "win_rate": "66.5%", "context": "2M", "license": "Proprietary", "badge": "超长窗口"},
            {"rank": 4, "model": "GPT-4o (Omni-Latest)", "org": "OpenAI", "elo": 1338, "win_rate": "65.1%", "context": "128K", "license": "Proprietary", "badge": "全能均衡"},
            {"rank": 5, "model": "DeepSeek-V3 (671B MoE)", "org": "DeepSeek", "elo": 1324, "win_rate": "63.2%", "context": "128K", "license": "MIT (Open)", "badge": "性价比旗舰"},
            {"rank": 6, "model": "Claude 3.5 Sonnet", "org": "Anthropic", "elo": 1319, "win_rate": "62.4%", "context": "200K", "license": "Proprietary", "badge": "经典王者"},
            {"rank": 7, "model": "Qwen 2.5 Max Preview", "org": "Alibaba", "elo": 1310, "win_rate": "60.8%", "context": "128K", "license": "Proprietary", "badge": "中文顶尖"},
            {"rank": 8, "model": "Llama 3.3 70B Instruct", "org": "Meta", "elo": 1292, "win_rate": "58.6%", "context": "128K", "license": "Llama 3.3 (Open)", "badge": "自部署首选"},
        ]
    },
    "coding": {
        "name": "代码编程与 Agent 天梯榜",
        "benchmark": "SWE-bench Verified / HumanEval / Aider Leaderboard",
        "description": "评估模型在真实 GitHub Issue 修复、端到端自动化软件重构与多文件工程中的代码解决能力。",
        "updated_at": "2026-09-18",
        "items": [
            {"rank": 1, "model": "Claude 3.7 Sonnet (Hybrid)", "org": "Anthropic", "swe_bench": "70.3%", "humaneval": "92.8%", "context": "200K", "cost": "$3.0 / $15.0", "badge": "编程霸主"},
            {"rank": 2, "model": "DeepSeek-R1 (Full MoE)", "org": "DeepSeek", "swe_bench": "68.2%", "humaneval": "91.5%", "context": "128K", "cost": "$0.55 / $2.19", "badge": "极致性价比"},
            {"rank": 3, "model": "Claude 3.5 Sonnet v2", "org": "Anthropic", "swe_bench": "65.0%", "humaneval": "90.2%", "context": "200K", "cost": "$3.0 / $15.0", "badge": "工业基石"},
            {"rank": 4, "model": "DeepSeek-V3 / V4 Flash", "org": "DeepSeek", "swe_bench": "58.4%", "humaneval": "88.6%", "context": "128K", "cost": "$0.14 / $0.28", "badge": "极低延迟"},
            {"rank": 5, "model": "GPT-4o (0806 / Coding)", "org": "OpenAI", "swe_bench": "54.8%", "humaneval": "87.4%", "context": "128K", "cost": "$2.5 / $10.0", "badge": "标准生态"},
            {"rank": 6, "model": "Qwen 2.5 Coder 32B Instruct", "org": "Alibaba", "swe_bench": "51.6%", "humaneval": "86.2%", "context": "128K", "cost": "单卡本地免费", "badge": "最强开源代码"},
            {"rank": 7, "model": "Gemini 2.0 Flash", "org": "Google", "swe_bench": "48.9%", "humaneval": "84.8%", "context": "1M", "cost": "$0.10 / $0.40", "badge": "秒级补全"},
        ]
    },
    "opensource": {
        "name": "全球顶尖开源大模型精选榜",
        "benchmark": "Open LLM Leaderboard / MMLU-Pro / Math500 / GPQA",
        "description": "专为可私有化部署、离线运行、支持商业化改编的模型设立的权威跑分榜。",
        "updated_at": "2026-09-18",
        "items": [
            {"rank": 1, "model": "DeepSeek-R1 (671B MoE)", "org": "DeepSeek", "params": "671B (37B active)", "license": "MIT", "math": "97.3%", "code": "91.5%", "badge": "全球最强开源"},
            {"rank": 2, "model": "DeepSeek-V3 (671B MoE)", "org": "DeepSeek", "params": "671B (37B active)", "license": "MIT", "math": "90.2%", "code": "88.6%", "badge": "通用全能开源"},
            {"rank": 3, "model": "Llama 3.3 70B Instruct", "org": "Meta", "params": "70B Dense", "license": "Llama 3.3", "math": "84.5%", "code": "82.4%", "badge": "70B黄金尺寸"},
            {"rank": 4, "model": "Qwen 2.5 72B Instruct", "org": "Alibaba", "params": "72B Dense", "license": "Apache 2.0", "math": "86.1%", "code": "85.0%", "badge": "中文开源王者"},
            {"rank": 5, "model": "Qwen 2.5 Coder 32B", "org": "Alibaba", "params": "32B Dense", "license": "Apache 2.0", "math": "81.0%", "code": "86.2%", "badge": "轻量级代码王"},
            {"rank": 6, "model": "Mistral Large 2 (123B)", "org": "Mistral AI", "params": "123B Dense", "license": "MNCL", "math": "82.6%", "code": "83.1%", "badge": "欧洲之星"},
        ]
    },
    "efficiency": {
        "name": "性价比与推理吞吐速度天梯榜",
        "benchmark": "Tokens/Sec Throughput & Pricing / Million Tokens",
        "description": "高并发业务、代码辅助补全、批量处理与轻量化 Agent 的最优架构推荐。",
        "updated_at": "2026-09-18",
        "items": [
            {"rank": 1, "model": "DeepSeek-V4 Flash / V3", "org": "DeepSeek", "speed": "75 tok/s", "input_cost": "$0.14", "output_cost": "$0.28", "cache_hit": "$0.014", "badge": "极速白菜价"},
            {"rank": 2, "model": "Gemini 2.0 Flash", "org": "Google", "speed": "115 tok/s", "input_cost": "$0.10", "output_cost": "$0.40", "cache_hit": "免费", "badge": "超光速响应"},
            {"rank": 3, "model": "GPT-4o mini", "org": "OpenAI", "speed": "65 tok/s", "input_cost": "$0.15", "output_cost": "$0.60", "cache_hit": "$0.075", "badge": "生态标杆"},
            {"rank": 4, "model": "Claude 3.5 Haiku", "org": "Anthropic", "speed": "60 tok/s", "input_cost": "$0.80", "output_cost": "$4.00", "cache_hit": "$0.08", "badge": "高质量轻量"},
            {"rank": 5, "model": "Qwen 2.5 7B GGUF (本地 4060)", "org": "Local llama.cpp", "speed": "45 tok/s", "input_cost": "$0.00", "output_cost": "$0.00", "cache_hit": "本地内存", "badge": "零云端成本"},
        ]
    },
    # 以下两榜为 datalearner.com 聚合评测快照（live 抓取失败时兜底，
    # 字段与 ai_fetcher._fetch_datalearner_boards 的 live 输出一致）
    "aa_index": {
        "name": "AA 智能指数榜",
        "benchmark": "Artificial Analysis 综合评测（datalearner 聚合）",
        "description": "第三方评测机构对知识/推理/数学等多维基准的加权综合分，反映模型硬能力。",
        "updated_at": "2026-09-19",
        "items": [
            {"rank": 1, "model": "Claude Fable 5.1", "org": "Anthropic", "score": 53, "thinking": "-", "badge": "综合能力第一"},
            {"rank": 2, "model": "Claude Fable 5.1", "org": "Anthropic", "score": 53, "thinking": "-", "badge": ""},
            {"rank": 3, "model": "GPT-6 Astra (max)", "org": "OpenAI", "score": 53, "thinking": "深度思考", "badge": ""},
            {"rank": 4, "model": "GPT-6 Astra (xhigh)", "org": "OpenAI", "score": 53, "thinking": "深度思考", "badge": ""},
            {"rank": 5, "model": "Claude Fable 5.1", "org": "Anthropic", "score": 51, "thinking": "-", "badge": ""},
            {"rank": 6, "model": "GPT-6 Astra (high)", "org": "OpenAI", "score": 51, "thinking": "深度思考", "badge": ""},
            {"rank": 7, "model": "Claude Opus 5 (max)", "org": "Anthropic", "score": 51, "thinking": "深度思考", "badge": ""},
            {"rank": 8, "model": "Claude Fable 5", "org": "Anthropic", "score": 50, "thinking": "-", "badge": ""},
            {"rank": 9, "model": "GPT-6 Astra (medium)", "org": "OpenAI", "score": 50, "thinking": "深度思考", "badge": ""},
            {"rank": 10, "model": "Claude Opus 5 (xhigh)", "org": "Anthropic", "score": 50, "thinking": "深度思考", "badge": ""},
        ]
    },
    "arena": {
        "name": "LMArena 人类偏好榜",
        "benchmark": "LMArena 盲测 Elo（datalearner 聚合）",
        "description": "大社区盲测投票 Elo——v2.1 因 lmarena 官方封 API 放弃的口径，经 datalearner 聚合页恢复。",
        "updated_at": "2026-09-19",
        "items": [
            {"rank": 1, "model": "Claude Fable 5", "org": "Anthropic", "elo": 1506, "ci": "+/-5", "votes": "30K", "license": "Proprietary", "badge": "人类偏好第一"},
            {"rank": 2, "model": "Claude Opus 4.6 (high)", "org": "Anthropic", "elo": 1505, "ci": "+/-4", "votes": "72K", "license": "Proprietary", "badge": ""},
            {"rank": 3, "model": "Opus 4.7 (high)", "org": "Anthropic", "elo": 1502, "ci": "+/-4", "votes": "60K", "license": "Proprietary", "badge": ""},
            {"rank": 4, "model": "Claude Fable 5.1 (max)", "org": "Anthropic", "elo": 1501, "ci": "+/-8", "votes": "5K", "license": "Proprietary", "badge": ""},
            {"rank": 5, "model": "Muse Spark 1.2 (xhigh)", "org": "Facebook AI 研究实验室", "elo": 1499, "ci": "+/-11", "votes": "3K", "license": "Proprietary", "badge": ""},
            {"rank": 6, "model": "Claude Opus 4.6", "org": "Anthropic", "elo": 1497, "ci": "+/-3", "votes": "76K", "license": "Proprietary", "badge": ""},
            {"rank": 7, "model": "Opus 4.7", "org": "Anthropic", "elo": 1494, "ci": "+/-4", "votes": "61K", "license": "Proprietary", "badge": ""},
            {"rank": 8, "model": "Muse Spark 1.1", "org": "Facebook AI 研究实验室", "elo": 1494, "ci": "+/-5", "votes": "27K", "license": "Proprietary", "badge": ""},
            {"rank": 9, "model": "Claude Opus 5 (high)", "org": "Anthropic", "elo": 1493, "ci": "+/-4", "votes": "42K", "license": "Proprietary", "badge": ""},
            {"rank": 10, "model": "gemini-3.8-flash-high", "org": "Google", "elo": 1493, "ci": "+/-9", "votes": "5K", "license": "Proprietary", "badge": ""},
        ]
    }
}


RANKINGS_CACHE_FILE = BASE_DIR / "ai_rankings_cache.json"
MODELS_CACHE_FILE = BASE_DIR / "ai_models_cache.json"

# 多模态榜不另抓数据：直接复用已抓好的 LMArena 视觉 / 文档榜缓存，
# 响应里保留请求的别名 ID，数据用 Arena 那份——**不复制缓存行**，
# 否则同一份数据会有两个可各自变旧的真源。
MEDIA_ARENA_ALIASES = {
    "multimodal__vision": "arena__vision__overall",
    "multimodal__document": "arena__document__overall",
}
MULTIMODAL_OPTIONS = [
    {"id": "multimodal__vision", "name": "视觉"},
    {"id": "multimodal__document", "name": "文档"},
]
MEDIA_PARENT_ALIASES = dict(ai_media_rankings.MEDIA_DEFAULT_BOARDS,
                            multimodal="multimodal__vision")

# 模型目录缺失时的兜底维度表（八项，与 ai_fetcher.AA_CAP_DIMS 同口径；
# 这里只放渲染必需的 key/label/unit，描述文案以抓取层写入的为准）
FALLBACK_CAP_DIMS = [
    {"key": "gdpval", "label": "真实世界工作任务", "english": "Agentic Real-World Work Tasks", "unit": "Elo"},
    {"key": "terminal", "label": "终端与编码智能体", "english": "Agentic Coding & Terminal Use", "unit": "%"},
    {"key": "scicode", "label": "科研编程", "english": "SciCode", "unit": "%"},
    {"key": "lcr", "label": "长上下文推理", "english": "Long Context Reasoning", "unit": "%"},
    {"key": "hle", "label": "推理与知识", "english": "Reasoning & Knowledge", "unit": "%"},
    {"key": "gpqa", "label": "科学推理", "english": "Scientific Reasoning", "unit": "%"},
    {"key": "critpt", "label": "批判性推理", "english": "CritPT", "unit": "%"},
    {"key": "omni", "label": "知识准确率", "english": "AA-Omniscience", "unit": "%"},
]


def load_ai_rankings(category="overall"):
    """获取指定分类的排行榜数据。

    优先读 ai_fetcher.py 抓取的真实数据缓存（ai_rankings_cache.json，
    source=live）；某榜缺失时回退内置快照 RANKINGS_DATA（source=snapshot）。
    能力榜（cap_*）没有内置快照——缓存缺失时返回空榜 + 明说原因，
    而不是静默拿 overall 快照顶替，否则用户会看到一张牌子对不上的表。
    """
    live = _load_rankings_cache()
    source_categories = {
        "arena": [{"id": key, "name": value.get("subnav_name") or value.get("name") or key}
                  for key, value in live.items() if key.startswith("arena__")],
        "livebench": [{"id": key, "name": value.get("subnav_name") or value.get("name") or key}
                      for key, value in live.items() if key.startswith("livebench__")],
        "media": _media_options(),
    }
    if category == "arena":
        category = ("arena__text__overall" if "arena__text__overall" in live
                    else "arena" if "arena" in live or "arena" in (RANKINGS_DATA or {})
                    else "arena__text__overall")
    elif category == "livebench":
        category = "livebench__overall"
    # 媒体主入口先落到默认细分榜；多模态再落到 Arena 视觉 / 文档缓存
    if category in MEDIA_PARENT_ALIASES:
        category = MEDIA_PARENT_ALIASES[category]
    if category in MEDIA_ARENA_ALIASES:
        arena_key = MEDIA_ARENA_ALIASES[category]
        alias_data = live.get(arena_key)
        if not isinstance(alias_data, dict) or not alias_data.get("items"):
            return {
                "status": "ok", "category": category, "data": None,
                "error": "该多模态榜尚未抓取（取自 LMArena 视觉 / 文档榜缓存），请点「刷新数据」重试",
                "available_categories": _rank_categories(),
                "capability_dimensions": _capability_dimensions(),
                "source_categories": source_categories,
            }
        return {
            "status": "ok",
            "category": category,
            "data": dict(alias_data, source="live"),
            "available_categories": _rank_categories(),
            "capability_dimensions": _capability_dimensions(),
            "source_categories": source_categories,
        }
    if category == "capability":
        # 前端「能力榜单」入口的别名：直接调 API 时落到默认维度，
        # 而不是掉进下面的兜底逻辑静默返回开源热度总榜（牌子对不上的表比空白更坏）
        category = "cap_gdpval"
    if category.startswith("cap_"):
        if category not in live:
            known = {d["key"] for d in _capability_dimensions()}
            dim = category[4:]
            err = ("未知的能力维度：" + dim if dim not in known else
                   "能力榜数据尚未抓取（缓存缺失），请点「立即更新」重试")
            return {
                "status": "ok", "category": category, "data": None, "error": err,
                "available_categories": _rank_categories(),
                "capability_dimensions": _capability_dimensions(),
                "source_categories": source_categories,
            }
    if category.startswith(("arena__", "livebench__")) and category not in live:
        return {
            "status": "ok", "category": category, "data": None,
            "error": "所选来源榜单尚未抓取，请点「刷新数据」重试",
            "available_categories": _rank_categories(),
            "capability_dimensions": _capability_dimensions(),
            "source_categories": source_categories,
        }
    if category.startswith(("video__", "image__", "audio__")) and (
            not isinstance(live.get(category), dict) or not live[category].get("items")):
        # 媒体榜没抓到时必须显式报错：掉进下面的兜底会静默换成开源热度总榜，
        # 牌子对不上的表比空白更坏（同 cap_ 分支的理由）。
        return {
            "status": "ok", "category": category, "data": None,
            "error": "所选媒体榜单尚未抓取，请点「刷新数据」重试",
            "available_categories": _rank_categories(),
            "capability_dimensions": _capability_dimensions(),
            "source_categories": source_categories,
        }
    if category not in (RANKINGS_DATA or {}) and category not in live:
        category = "overall"
    if category in live and live[category].get("items"):
        data = live[category]
    else:
        data = dict(RANKINGS_DATA.get(category) or RANKINGS_DATA["overall"])
        data.setdefault("source", "snapshot")
    return {
        "status": "ok",
        "category": category,
        "data": data,
        "available_categories": _rank_categories(),
        "capability_dimensions": _capability_dimensions(),
        "source_categories": source_categories,
    }


def _rank_categories():
    base = [
        {"id": "aa_index", "name": "AA 智能指数榜（全模型）", "icon": "target"},
        {"id": "capability", "name": "能力榜单（八维分项）", "icon": "chart-bar"},
        {"id": "arena", "name": "LMArena 偏好榜", "icon": "users"},
        {"id": "livebench", "name": "LiveBench 基准榜", "icon": "chart-line"},
        {"id": "video", "name": "视频生成榜", "icon": "video"},
        {"id": "audio", "name": "音频模型榜", "icon": "mic"},
        {"id": "image", "name": "生图模型榜", "icon": "image"},
        {"id": "multimodal", "name": "多模态榜", "icon": "layers"},
        {"id": "overall", "name": "开源热度榜", "icon": "trophy"},
        {"id": "coding", "name": "代码模型榜", "icon": "code"},
        {"id": "opensource", "name": "开源生态榜", "icon": "github"},
        {"id": "efficiency", "name": "性价比与速度榜", "icon": "zap"},
    ]
    return base


def _media_options():
    """四个媒体主入口各自的细分榜选项；多模态复用 Arena 视觉 / 文档。"""
    options = {key: [dict(opt) for opt in rows]
               for key, rows in ai_media_rankings.MEDIA_CATEGORY_OPTIONS.items()}
    options["multimodal"] = [dict(opt) for opt in MULTIMODAL_OPTIONS]
    return options


def _capability_dimensions():
    """供前端渲染能力榜的维度 chip。以模型目录里的维度表为准（抓取时写入），
    缓存缺失才退回内置一份，保证前端永远拿得到八个维度。"""
    try:
        with open(MODELS_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        dims = ((cached.get("sources") or {}).get("aa") or cached).get("dimensions") or []
        if len(dims) >= 8:
            return dims
    except Exception:
        pass
    return FALLBACK_CAP_DIMS


def _load_rankings_cache():
    try:
        with open(RANKINGS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("rankings", {})
    except Exception:
        return {}


# ================================================== 3. 模型对比目录
def load_ai_models(q="", limit=300, source="aa", board="text"):
    """按数据库读取模型对比目录；旧版单目录缓存继续视为 AA 来源。"""
    if source not in ("aa", "arena", "livebench"):
        return {"status": "error", "error": "未知模型数据源: " + str(source),
                "total": 0, "updated_at": "", "dimensions": [], "items": []}
    empty_dims = FALLBACK_CAP_DIMS if source == "aa" else []
    empty = {"status": "ok", "source": source, "board": board, "total": 0,
             "updated_at": "", "source_updated_at": "", "published_at": "",
             "dimensions": empty_dims, "items": [], "board_options": []}
    if not MODELS_CACHE_FILE.exists():
        return empty
    try:
        with open(MODELS_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return empty
    sources = cached.get("sources") if isinstance(cached, dict) else None
    if isinstance(sources, dict):
        source_data = sources.get(source) or {}
    else:
        source_data = cached if source == "aa" else {}
    board_options = source_data.get("board_options") or []
    if source == "arena":
        boards = source_data.get("boards") or {}
        board_data = boards.get(board)
        if not board_data and boards:
            board_data = boards.get("text") or next(iter(boards.values()))
            board = board_data.get("board") or board
        if not board_data:
            return empty
        catalog = board_data
    else:
        catalog = source_data
    models = catalog.get("models") or []
    if q:
        s = q.lower().strip()
        models = [m for m in models
                  if s in (m.get("name") or "").lower()
                  or s in (m.get("org") or "").lower()]
    try:
        limit = max(1, min(int(limit), 5000))
    except (TypeError, ValueError):
        limit = 300
    return {
        "status": "ok",
        "source": source,
        "board": board if source == "arena" else None,
        "total": len(models),
        "updated_at": catalog.get("updated_at") or source_data.get("updated_at", ""),
        "source_updated_at": source_data.get("updated_at", ""),
        "published_at": catalog.get("published_at") or source_data.get("published_at", ""),
        "dimensions": catalog.get("dimensions") or empty_dims,
        "board_options": board_options,
        "items": models[:limit],
    }


if __name__ == "__main__":
    print("AI 新闻数:", load_ai_news()["total"])
    print("每日要闻数:", load_ai_briefings(5)["total"])
    print("综合榜首名:", load_ai_rankings("overall")["data"]["items"][0]["model"])
    print("代码榜首名:", load_ai_rankings("coding")["data"]["items"][0]["model"])
    print("能力榜:", load_ai_rankings("cap_gpqa")["data"]["items"][0])
    print("模型目录:", load_ai_models()["total"])
