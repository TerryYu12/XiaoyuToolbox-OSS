"""公共媒体模型榜（视频 / 生图 / 音频）适配器。

全部结论来自 2026-09-24 对真实上游的实测，勿凭猜测改动本文件里的结构假设：

1. **AA 媒体榜页很大、很慢**：单页 2.1~2.9 MB，代理下载 **31~80 秒**。
   旧默认超时（`_http_get` 12s、`_rsc_stream` 30s）**必然失败**，表现为
   `SSL: UNEXPECTED_EOF_WHILE_READING`。曾被误诊为「上游 SSL 问题」——
   真因是超时值小于页面传输时间。本模块统一用 `PAGE_TIMEOUT`。
2. **不能用「长度最大」或「第 N 个」数组选全局榜**：生图页有 20 个
   `{"formatted":…,"values":…}` 数组，长度散乱（161/99/111/120/151/91/89…）。
   正解是解析 RSC 引用键：`"withAudioData":"$Q3a"` → 帧 `3a` → 帧内是
   `[[null, rows], ["photorealistic", rows], …]` 的 `[标签, 行]` 对列表，
   **`null` 标签那一组就是「All」全局榜**。
   引用里 `$` 后面可能带非十六进制前缀字母（`Q3a`），解析时要剥掉再找帧号。
3. **ASR 用 Hugging Face 数据集**：67 行；`avg` 是 WER（**越低越好**）、
   `RTFx` 越高越快、`Size (B)` 是十亿参数、`License == "Proprietary"` 即闭源。
4. **语音对语音页结构不同**：扁平 `{elo, lower95ci, modelKey, creatorName…}`
   数组，没有 `formatted/values` 包装，走 `normalize_s2s_rows`。

口径：各榜保留来源原分数与方向，**不跨榜不跨任务合成**；缺资料一律「未知」，不按模型名猜。
"""

import inspect
import json
import re
import time
from urllib.parse import urlencode

# AA 媒体页实测 31~80 秒，给足余量；不要调回旧的 30 秒。
PAGE_TIMEOUT = 120
MAX_ROWS = 100

ASR_DATASET = "hf-audio/open-asr-leaderboard-results"
DATASET_API = "https://datasets-server.huggingface.co"
ASR_SPLITS_URL = DATASET_API + "/splits?dataset=hf-audio%2Fopen-asr-leaderboard-results"
ASR_ROWS_URL = DATASET_API + "/rows?"

# AA 公开榜页（已批准 spec 里的 URL，勿臆造路径）
MEDIA_CATEGORY_OPTIONS = {
    "video": [
        {"id": "video__text_to_video__audio", "name": "文生视频 · 有音频"},
        {"id": "video__text_to_video__no_audio", "name": "文生视频 · 无音频"},
        {"id": "video__image_to_video__audio", "name": "图生视频 · 有音频"},
        {"id": "video__image_to_video__no_audio", "name": "图生视频 · 无音频"},
    ],
    "image": [
        {"id": "image__text_to_image", "name": "文生图"},
        {"id": "image__editing", "name": "图片编辑"},
    ],
    "audio": [
        {"id": "audio__asr", "name": "语音识别"},
        {"id": "audio__tts", "name": "语音合成"},
        {"id": "audio__music_instrumental", "name": "纯音乐生成"},
        {"id": "audio__music_vocals", "name": "人声音乐生成"},
    ],
}

MEDIA_DEFAULT_BOARDS = {key: rows[0]["id"] for key, rows in MEDIA_CATEGORY_OPTIONS.items()}

AA_BOARD_URLS = {
    "video__text_to_video__audio": "https://artificialanalysis.ai/video/leaderboard/text-to-video",
    "video__text_to_video__no_audio": "https://artificialanalysis.ai/video/leaderboard/text-to-video",
    "video__image_to_video__audio": "https://artificialanalysis.ai/video/leaderboard/image-to-video",
    "video__image_to_video__no_audio": "https://artificialanalysis.ai/video/leaderboard/image-to-video",
    "image__text_to_image": "https://artificialanalysis.ai/image/leaderboard/text-to-image",
    "image__editing": "https://artificialanalysis.ai/image/leaderboard/editing",
    "audio__tts": "https://artificialanalysis.ai/text-to-speech/leaderboard/provider-voice",
    "audio__music_instrumental": "https://artificialanalysis.ai/music/leaderboard/instrumental",
    "audio__music_vocals": "https://artificialanalysis.ai/music/leaderboard/vocals",
}
# 语音对语音（/speech-to-speech/arena）暂缓接入（2026-09-24 master 决定）：
# 该页结构是扁平 `{elo, lower95ci, modelKey…}` 数组、没有 ref 键可供定位全局榜，
# 实测按数组扫描取不到稳定结果（报错「未取到语音对语音榜单数组」）。
# 解析器 `normalize_s2s_rows` 与 `_flat_elo_arrays` 保留：结构已验证，等确认取数位置再接。
S2S_PAGE_URL = "https://artificialanalysis.ai/speech-to-speech/arena"

# board → (页面标识, 音频档)。音频档取哪一组由页面 ref 键决定，靠 ref 而非顺序。
MEDIA_PAGE_BOARDS = {
    "video__text_to_video__audio": ("text-to-video", "withAudio"),
    "video__text_to_video__no_audio": ("text-to-video", "noAudio"),
    "video__image_to_video__audio": ("image-to-video", "withAudio"),
    "video__image_to_video__no_audio": ("image-to-video", "noAudio"),
    "image__text_to_image": ("text-to-image", "global"),
    "image__editing": ("editing", "global"),
    "audio__tts": ("provider-voice", "global"),
    "audio__music_instrumental": ("instrumental", "global"),
    "audio__music_vocals": ("vocals", "global"),
}

# ref 键候选名：视频页有两个音频档；其余页先试通用名，都取不到才退回首数组扫描。
_AUDIO_REF_NAMES = {
    "withAudio": ("withAudioData", "withAudio"),
    "noAudio": ("withoutAudioData", "noAudioData", "noAudio"),
}
_GENERIC_REF_NAMES = (
    "initialData", "data", "rows", "leaderboardData", "modelsData", "tableData",
    "arenaData", "eloData", "leaderboard",
)

_FRAME_RE_TMPL = r"(?:^|\n)%s:"


# ------------------------------------------------------------------ 小工具

def _call_with_timeout(fn, url, timeout):
    """注入的取数回调可能不带 timeout 形参（测试替身），按签名决定怎么调。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "timeout" in params:
        return fn(url, timeout=timeout)
    return fn(url)


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def _text(value, fallback="未知"):
    if value is None or value == "" or value == "$undefined":
        return fallback
    return str(value)


def _pick(val, *keys):
    for key in keys:
        if isinstance(val, dict) and val.get(key) not in (None, ""):
            return val[key]
    return None


def _rank_rows(items, direction, limit=MAX_ROWS):
    valid = [it for it in items if isinstance(it.get("score"), (int, float))]
    valid.sort(key=lambda it: it["score"], reverse=(direction == "higher"))
    return [dict(it, rank=i + 1) for i, it in enumerate(valid[:limit])]


def _board_meta(board_id):
    for rows in MEDIA_CATEGORY_OPTIONS.values():
        for opt in rows:
            if opt["id"] == board_id:
                return opt["name"], AA_BOARD_URLS.get(board_id, "")
    return board_id, AA_BOARD_URLS.get(board_id, "")


# ------------------------------------------------------------------ ASR

def normalize_asr_rows(rows, limit=MAX_ROWS):
    """WER 越低越好；`Proprietary` 是数据集的闭源信号，其余只算「许可已知」。"""
    items = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        row = raw.get("row") if isinstance(raw.get("row"), dict) else raw
        model = row.get("model")
        wer = _num(row.get("avg"))
        if not model or wer is None:
            continue
        license_raw = row.get("License")
        license_text = _text(license_raw)
        open_status = "闭源" if license_text.strip().lower() == "proprietary" else "未知"
        model_text = str(model)
        disclosure = str(row.get("Training data disclosure") or "")
        repo = None
        if "huggingface.co/" in disclosure:
            repo = disclosure.split("huggingface.co/", 1)[1].split("#")[0].strip("/") or None
        elif "/" in model_text:
            repo = model_text.strip("/")
        items.append({
            "model": model_text,
            "org": model_text.split("/")[0] if "/" in model_text else "未知",
            "score": round(wer, 4),
            "unit": "WER",
            "direction": "lower",
            "open_status": open_status,
            "parameters_b": _num(row.get("Size (B)")),
            "license": license_text,
            "released": None,
            "price": None,
            "ci": None,
            "samples": None,
            "source_url": "https://huggingface.co/datasets/" + ASR_DATASET,
            "details": {
                "rtfx": _num(row.get("RTFx")),
                "languages": _num(row.get("# Languages")),
                "encoder": _text(row.get("Encoder")),
                "decoder": _text(row.get("Decoder")),
                "repository_url": ("https://huggingface.co/" + repo) if repo else None,
            },
        })
    return _rank_rows(items, "lower", limit)


def fetch_asr_board(dataset_api_json, limit=MAX_ROWS):
    """先 splits 确认 default/train，再取一页 rows；空页或缺列视为失败。"""
    splits = dataset_api_json(ASR_SPLITS_URL)
    pairs = {(s.get("config"), s.get("split")) for s in (splits or {}).get("splits", [])}
    if ("default", "train") not in pairs:
        raise RuntimeError("ASR 数据集缺少 default/train 分片")
    params = urlencode({"dataset": ASR_DATASET, "config": "default",
                        "split": "train", "offset": 0, "length": limit})
    payload = dataset_api_json(ASR_ROWS_URL + params)
    rows = [r.get("row") for r in (payload or {}).get("rows", []) if isinstance(r, dict)]
    items = normalize_asr_rows(rows, limit)
    if not items:
        raise RuntimeError("ASR 数据集返回空页或缺少 model/avg 列")
    name, url = _board_meta("audio__asr")
    return {
        "name": name,
        "benchmark": "Open ASR Leaderboard · 平均 WER（越低越好）",
        "description": "Hugging Face Open ASR 汇总的语音识别评测：WER 越低越好，RTFx 越高越快。",
        "unit": "WER",
        "direction": "lower",
        "items": items,
        "num_rows_total": (payload or {}).get("num_rows_total"),
        "source_url": url,
        "source": "live",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ------------------------------------------------------------------ AA 页面解析

def _frame_value(stream, ref_id):
    """`$Q3a` → 帧 3a 的 JSON 值。`$` 后可能带非十六进制前缀字母，剥掉重试。"""
    for fid in (ref_id, ref_id[1:], ref_id[1:].lower()):
        if not fid:
            continue
        m = re.search(_FRAME_RE_TMPL % re.escape(fid), stream)
        if not m:
            continue
        try:
            val, _ = json.JSONDecoder().raw_decode(stream[m.end():m.end() + 4_000_000].lstrip())
        except Exception:
            continue
        if isinstance(val, list) and val:
            return val
    return None


def _rows_from_value(val, stream=None, depth=0):
    """帧值 / 预解析值 → 「All」全局榜行列表。

    真实帧形如 `[[null, rows], ["photorealistic", rows], …]`：`null` 标签即 All。
    TTS 页更绕一层：`[[null, "$Q33"], …]`——行数据在另一个帧里，要顺着引用再解一层。
    """
    if depth > 3:
        return []
    if isinstance(val, str) and val.startswith("$") and stream is not None:
        return _rows_from_value(_frame_value(stream, val[1:]), stream, depth + 1)
    if not isinstance(val, list) or not val:
        return []
    if isinstance(val[0], dict):
        return val
    pairs = [p for p in val if isinstance(p, list) and len(p) == 2]
    if pairs:
        for tag, payload in pairs:
            if tag is None or str(tag).lower() in ("all", "overall"):
                rows = _rows_from_value(payload, stream, depth + 1)
                if rows:
                    return rows
        return []
    for p in val:
        if isinstance(p, list):
            found = _rows_from_value(p, stream, depth + 1)
            if found:
                return found
    return []


def _formatted_arrays(stream):
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(r'\[\{"', stream):
        try:
            val, _ = dec.raw_decode(stream, m.start())
        except Exception:
            continue
        if (isinstance(val, list) and val and isinstance(val[0], dict)
                and "formatted" in val[0] and "values" in val[0]):
            out.append(val)
    return out


def _flat_elo_arrays(stream):
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(r'\[\{"', stream):
        try:
            val, _ = dec.raw_decode(stream, m.start())
        except Exception:
            continue
        if (isinstance(val, list) and val and isinstance(val[0], dict)
                and "elo" in val[0] and "formatted" not in val[0]):
            out.append(val)
    return out


def select_rows(page, mode, page_key=""):
    """取该 board 的全局榜行列表。

    三种输入都支持：① 测试替身/预解析 `{"tables":[…]}`；② 真实 RSC 流字符串；
    ③ 已解析的帧值。**音频档只认 ref 键**，ref 缺失时报错而不是猜一个。
    """
    if isinstance(page, dict):
        tables = page.get("tables")
        if not isinstance(tables, list):
            return []
        for t in tables:
            if not isinstance(t, dict):
                continue
            tab = t.get("tab") or "All"
            audio = t.get("audioMode")
            if tab != "All":
                continue
            if mode == "withAudio" and audio not in ("with_audio", "withAudio"):
                continue
            if mode == "noAudio" and audio not in ("without_audio", "noAudio", "no_audio"):
                continue
            if mode == "global" and audio not in (None, "with_audio", "withAudio"):
                continue
            return t.get("rows") or []
        return []
    if isinstance(page, list):
        return _rows_from_value(page, None)
    if not isinstance(page, str):
        return []

    names = _AUDIO_REF_NAMES.get(mode, ()) + (() if mode in _AUDIO_REF_NAMES else _GENERIC_REF_NAMES)
    for m in re.finditer(r'"([A-Za-z][A-Za-z0-9_]{1,40})"\s*:\s*"\$([A-Za-z0-9]{1,6})"', page):
        if m.group(1) not in names:
            continue
        rows = _rows_from_value(_frame_value(page, m.group(2)), page)
        if rows:
            return rows
    if mode in _AUDIO_REF_NAMES:
        # 视频页的音频档只能靠 ref 区分，取不到宁可报错，也不拿另一档冒充
        return []
    arrays = _formatted_arrays(page)
    return arrays[0] if arrays else []


def normalize_aa_rows(board_id, rows, limit=MAX_ROWS):
    """AA 页行 → 统一条目（单位 Elo，越高越好）。兼容真实字段与文档字段两套命名。"""
    source_url = AA_BOARD_URLS.get(board_id, "")
    items, seen = [], set()
    for entry in rows or []:
        if not isinstance(entry, dict):
            continue
        fmt = entry.get("formatted") or {}
        val = entry.get("values") or entry
        elo = _num(_pick(val, "elo")) if _pick(val, "elo") is not None else _num(fmt.get("elo"))
        if elo is None:
            continue
        key = (_pick(val, "id") or str(_pick(val, "name")) + str(_pick(val, "releaseDate")))
        if key in seen:
            continue
        seen.add(key)
        creator = val.get("creator") if isinstance(val.get("creator"), dict) else {}
        open_url = _pick(val, "openWeightsUrl", "open_weight_url")
        if open_url or val.get("openWeights") is True:
            open_status = "开放权重"
        elif val.get("openWeights") is False or str(_pick(val, "openWeightsLabel") or "").lower() == "proprietary":
            open_status = "闭源"
        else:
            open_status = "未知"
        ci = _pick(val, "ci")
        if not ci:
            lo, hi = _num(val.get("ciLower")), _num(val.get("ciUpper"))
            ci = "%s/%s" % (round(lo), round(hi)) if lo is not None and hi is not None else None
        price = _pick(val, "price", "pricePerMinute", "pricePer1mCharacters", "pricePer1kImages")
        price_unit = {"pricePerMinute": "/min", "pricePer1mCharacters": "/1M 字符",
                      "pricePer1kImages": "/1K 图"}.get(
            next((k for k in ("pricePerMinute", "pricePer1mCharacters", "pricePer1kImages")
                  if val.get(k) is not None), ""), "")
        items.append({
            "source_id": _pick(val, "id"),
            "model": _text(_pick(val, "name") or fmt.get("name")),
            "org": _text(_pick(creator, "name") or _pick(val, "creator") or fmt.get("creator")),
            "score": round(elo, 1),
            "unit": "Elo",
            "direction": "higher",
            "ci": ci,
            "samples": int(_num(_pick(val, "appearances", "samples")) or 0) or None,
            "released": (_text(_pick(val, "releaseDate", "released")
                                or _pick(fmt, "released", "release_date")) or None),
            "price": ("$%s %s" % (price, price_unit)).strip() if price is not None else _pick(fmt, "price"),
            "open_status": open_status,
            "parameters_b": None,
            "license": "未知",
            "source_url": source_url,
            "details": {
                "repository_url": open_url,
                "win_rate": _num(_pick(val, "winRate")),
                "voices": int(_num(_pick(val, "voiceCount", "voices")) or 0) or None,
                "note": _text(_pick(val, "note"), "") or "",
            },
        })
    return _rank_rows(items, "higher", limit)


def normalize_s2s_rows(rows, limit=MAX_ROWS):
    """语音对语音页：扁平 `{elo, lower95ci, modelKey…}`，无 formatted 包装。

    **当前没有调用方**：该榜暂缓接入（见 `S2S_PAGE_URL` 处说明）。留着是因为
    字段口径已经对着真页面核实过，接入时不用再摸一遍。
    """
    source_url = S2S_PAGE_URL
    items = []
    for entry in rows or []:
        if not isinstance(entry, dict):
            continue
        elo = _num(entry.get("elo"))
        if elo is None:
            continue
        lo, hi = _num(entry.get("lower95ci")), _num(entry.get("upper95ci"))
        items.append({
            "source_id": entry.get("id") or entry.get("modelKey"),
            "model": _text(entry.get("name")),
            "org": _text(entry.get("creatorName")),
            "score": round(elo, 1),
            "unit": "Elo",
            "direction": "higher",
            "ci": "%s/%s" % (round(lo), round(hi)) if lo is not None and hi is not None else None,
            "samples": int(_num(entry.get("appearances")) or 0) or None,
            "released": None,
            "price": None,
            "open_status": "未知",
            "parameters_b": None,
            "license": "未知",
            "source_url": source_url,
            "details": {"model_key": entry.get("modelKey")},
        })
    return _rank_rows(items, "higher", limit)


# ------------------------------------------------------------------ 模型资料

_REPO_API = "https://huggingface.co/api/models/"


def _repo_id_from_url(url):
    if not isinstance(url, str) or "huggingface.co/" not in url:
        return None
    repo = url.split("huggingface.co/", 1)[1].split("#")[0].split("?")[0].strip("/")
    return repo or None


def enrich_model_metadata(items, metadata_cache, fetch_metadata):
    """按仓库 ID 富化参数量与许可；同一个仓库只请求一次，失败保留榜单行并标「未知」。

    参数量只认模型卡明确值（`safetensors.parameters` 求和或 `config.num_parameters`），
    **不按模型名或文件体积估算**。
    """
    cache = dict(metadata_cache or {})
    for item in items or []:
        if not isinstance(item, dict):
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        repo = _repo_id_from_url(details.get("repository_url"))
        if not repo:
            continue
        if repo in cache and cache[repo] is not None:
            info = cache[repo]
        else:
            try:
                info = fetch_metadata(repo)
            except Exception:
                info = None
            cache[repo] = info
        if not isinstance(info, dict):
            continue
        params = None
        sizes = (info.get("safetensors") or {}).get("parameters")
        if isinstance(sizes, dict):
            nums = [v for v in sizes.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if nums:
                params = sum(nums)
        if params is None:
            params = _num((info.get("config") or {}).get("num_parameters"))
        if params:
            item["parameters_b"] = round(params / 1_000_000_000, 3)
        license_value = info.get("license")
        if license_value:
            item["license"] = str(license_value)
        # 只有「非专有许可 + 可解析到公开仓库」才敢说开放权重
        if (item.get("open_status") == "未知" and item.get("license") not in (None, "未知")
                and str(item["license"]).strip().lower() not in ("proprietary", "other")):
            item["open_status"] = "开放权重"
    return items, cache


def fetch_repo_metadata(http_get, repo, timeout=30):
    data = _call_with_timeout(http_get, _REPO_API + repo, timeout)
    if isinstance(data, dict):
        return data
    return json.loads(data.decode("utf-8", "replace"))


# ------------------------------------------------------------------ 总入口

def fetch_media_rankings(http_get, rsc_stream, dataset_api_json, metadata_cache=None,
                         limit=MAX_ROWS):
    """抓全部媒体细分榜。

    返回 `(boards, metadata_cache, errors)`：`boards[board_id]` 是可直接进
    `ai_rankings_cache.json` 的榜单字典（items 非空才算成功）；单个榜失败只记进
    `errors` 并保留旧缓存，不影响其它榜。同一 URL 只抓一次。
    """
    boards, errors = {}, {}
    pages = {}

    for board_id, (page_key, mode) in MEDIA_PAGE_BOARDS.items():
        url = AA_BOARD_URLS.get(board_id)
        if not url:
            errors[board_id] = "缺少源 URL"
            continue
        try:
            if url not in pages:
                pages[url] = _call_with_timeout(rsc_stream, url, PAGE_TIMEOUT)
            rows = select_rows(pages[url], mode, page_key)
            if not rows:
                raise RuntimeError("未取到全局榜数组（页面结构可能变更）")
            items = normalize_aa_rows(board_id, rows, limit)
            if not items:
                raise RuntimeError("全局榜数组为空或缺少 Elo 字段")
            name, source_url = _board_meta(board_id)
            boards[board_id] = {
                "name": name,
                "benchmark": "Artificial Analysis 公开榜（Elo）",
                "description": "Artificial Analysis 公开榜单数据，保留原始 Elo 与置信区间，越高越好。",
                "unit": "Elo",
                "direction": "higher",
                "items": items,
                "source_url": source_url,
                "source": "live",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            errors[board_id] = "%s: %s" % (type(e).__name__, e)

    try:
        boards["audio__asr"] = fetch_asr_board(dataset_api_json, limit)
    except Exception as e:
        errors["audio__asr"] = "%s: %s" % (type(e).__name__, e)

    all_items = [it for b in boards.values() for it in (b.get("items") or [])]
    _, metadata = enrich_model_metadata(
        all_items, metadata_cache, lambda repo: fetch_repo_metadata(http_get, repo))
    return boards, metadata, errors
