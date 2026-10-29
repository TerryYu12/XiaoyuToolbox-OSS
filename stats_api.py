# -*- coding: utf-8 -*-
"""Token 统计取数模块（只读数据源，产出前端契约里的 JSON）。

数据源：
  A) Hermes 各 agent（profile）state.db -> sessions 表
     路径: <hermes_home>/state.db 及 <hermes_home>/profiles/<name>/state.db
     字段: model, input_tokens, output_tokens, cache_read_tokens,
           cache_write_tokens, reasoning_tokens, estimated_cost_usd,
           actual_cost_usd, started_at, ended_at, message_count,
           tool_call_count, api_call_count
  B) CCS (CC Switch) cc-switch.db
     表: proxy_request_logs（逐请求，含 model/tokens/成本/延迟）
         usage_daily_rollups（按日聚合，含成本）
         model_pricing（价格表）
  C) Codex CLI 本地 rollout 会话文件（覆盖全部线路的权威记账）
     ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
     ~/.codex/archived_sessions/rollout-*.jsonl
     不管请求走 CCS(15721)、Codex++(57321) 还是官方直连，Codex 都把
     token 记账写进该文件——Codex++ 是纯转发代理不落账，CCS 亦只记
     它接管期间的流量，故 Codex 的 token 以此为准、成本以 CCS 记账
     为部分口径（见 codex_stats）。

口径（重要，实测踩坑）：
  - 命中率 = cache_read / (input + cache_read + cache_creation)
  - CCS 的 input_token_semantics 必须逐行区分（schema v13）：
      0 legacy = 入库时已减 cache reads（input 不含 cache_read）
      1 total-inclusive = input_tokens **已包含** cache_read+cache_creation
        （Codex/Gemini 原生上报格式，直接加 cache 会双算）
      2 fresh = 纯 input（rollups 全部标 2，claude 系原生 2）
    汇总前统一折算成「纯 input」，total = 纯input+output+cache_read+cache_creation
  - Hermes 的 input_tokens 不含 cache_read
  - Codex rollout 的 input_tokens 已含 cached（OpenAI 风格），同 s=1 折算
"""
import glob
import hashlib
import io
import math
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import datetime

HERMES = os.path.expanduser(r"~\AppData\Local\hermes")
CCS_DB = os.path.expanduser(r"~\.cc-switch\cc-switch.db")
# CC 适配器地址：provider 配置里出现它就说明这个 provider 走的是「本机 → 3050」
# 池账号的 key 形态（new-api 的 user_ key），用来从 provider 配置里取 key 算指纹。
_CC_KEY_RE = re.compile(r"user_[A-Za-z0-9]{16,}")
def _discover_agents():
    """自动发现 Hermes 档案：default + profiles/<name>/state.db 存在的所有档案。

    2026-09-23 教训：硬编码名单漏了 `xiaoyu`（当时最大的档案，今天 175M tok
    全部不在仪表盘统计里）。新档案随时会建，名单必须跟着磁盘走。
    """
    names = ["default"]
    pdir = os.path.join(HERMES, "profiles")
    try:
        for d in sorted(os.listdir(pdir)):
            if os.path.exists(os.path.join(pdir, d, "state.db")):
                names.append(d)
    except OSError:
        pass
    return names


AGENTS = _discover_agents()
_INITIAL_AGENTS = list(AGENTS)


def _active_agents():
    """获取当前可用的 Hermes 档案列表（优先尊重外部 patch/测试打桩，否则动态发现）。"""
    global AGENTS
    if AGENTS and AGENTS != _INITIAL_AGENTS:
        return AGENTS
    return _discover_agents()
# 成本异常阈值（USD）：实测 terra 档案有一条 1.83e7 的脏估值，
# 单档案成本超过该值判为异常，不计入汇总（前端应标灰/警示）
COST_SANE_LIMIT = 1000.0


def _ro(path):
    """只读打开（不锁库，避免与运行中的程序冲突）。"""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _agent_db(name):
    if name == "default":
        return os.path.join(HERMES, "state.db")
    return os.path.join(HERMES, "profiles", name, "state.db")


def _sum_row(cur, sql, args=()):
    try:
        r = cur.execute(sql, args).fetchone()
        return r if r else ()
    except Exception:
        return ()


def hermes_agents(since_ts=None):
    """各 agent（profile）的 token 汇总。
    优先聚合 session_model_usage 获取多模型/子任务真实消耗；
    时间窗对齐最新活跃时刻（MAX(last_activity_at, ended_at, started_at)），
    彻底解决长会话跨天/未结束在今日窗口被漏计的问题。
    """
    out = []
    for name in _active_agents():
        db = _agent_db(name)
        if not os.path.exists(db):
            continue
        try:
            con = _ro(db)
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            has_smu = "session_model_usage" in tables
            sess_cols = [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]
            has_act = "last_activity_at" in sess_cols
            has_calls = "api_call_count" in sess_cols

            where_sess, args = "", []
            if since_ts:
                if has_act:
                    where_sess = "WHERE MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0)) >= ?"
                else:
                    where_sess = "WHERE COALESCE(ended_at, started_at, 0) >= ?"
                args = [int(since_ts)]

            call_col = "SUM(COALESCE(api_call_count,0))" if has_calls else "0"
            row = _sum_row(cur, f"""
                SELECT COUNT(*),
                       SUM(COALESCE(input_tokens,0)),
                       SUM(COALESCE(output_tokens,0)),
                       SUM(COALESCE(cache_read_tokens,0)),
                       SUM(COALESCE(cache_write_tokens,0)),
                       SUM(COALESCE(reasoning_tokens,0)),
                       SUM(COALESCE(estimated_cost_usd,0)),
                       SUM(COALESCE(actual_cost_usd,0)),
                       SUM(COALESCE(message_count,0)),
                       SUM(COALESCE(tool_call_count,0)),
                       {call_col}
                FROM sessions {where_sess}""", args)

            if not row or not row[0]:
                con.close()
                continue

            inp = row[1] or 0
            out_t = row[2] or 0
            cr = row[3] or 0
            cw = row[4] or 0
            reasoning = row[5] or 0
            cost = round((row[6] or 0), 6)
            actual_cost = round((row[7] or 0), 6)
            msgs = row[8] or 0
            tool_calls = row[9] or 0
            api_calls = (row[10] or 0) if len(row) > 10 else 0

            models = []
            if has_smu:
                smu_where, smu_args = "", []
                if since_ts:
                    if has_act:
                        smu_where = """WHERE session_id IN (
                            SELECT id FROM sessions
                            WHERE MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0)) >= ?
                        )"""
                    else:
                        smu_where = """WHERE session_id IN (
                            SELECT id FROM sessions
                            WHERE COALESCE(ended_at, started_at, 0) >= ?
                        )"""
                    smu_args = [int(since_ts)]

                smu_tot = cur.execute(f"""
                    SELECT SUM(COALESCE(input_tokens,0)),
                           SUM(COALESCE(output_tokens,0)),
                           SUM(COALESCE(cache_read_tokens,0)),
                           SUM(COALESCE(cache_write_tokens,0)),
                           SUM(COALESCE(reasoning_tokens,0)),
                           SUM(COALESCE(api_call_count,0))
                    FROM session_model_usage {smu_where}""", smu_args).fetchone()
                if smu_tot and smu_tot[0] is not None:
                    inp = smu_tot[0] or 0
                    out_t = smu_tot[1] or 0
                    cr = smu_tot[2] or 0
                    cw = smu_tot[3] or 0
                    reasoning = smu_tot[4] or 0
                    if smu_tot[5]:
                        api_calls = max(api_calls, smu_tot[5] or 0)

                for m in cur.execute(f"""
                        SELECT model, COUNT(DISTINCT session_id),
                               SUM(COALESCE(input_tokens,0)),
                               SUM(COALESCE(output_tokens,0)),
                               SUM(COALESCE(cache_read_tokens,0))
                        FROM session_model_usage {smu_where}
                        GROUP BY model
                        ORDER BY 3 DESC LIMIT 8""", smu_args):
                    models.append({
                        "model": m[0] or "(unknown)", "sessions": m[1],
                        "input": m[2] or 0, "output": m[3] or 0,
                        "cache_read": m[4] or 0})
            else:
                for m in cur.execute(f"""
                        SELECT model, COUNT(*),
                               SUM(COALESCE(input_tokens,0)),
                               SUM(COALESCE(output_tokens,0)),
                               SUM(COALESCE(cache_read_tokens,0))
                        FROM sessions {where_sess} GROUP BY model
                        ORDER BY 3 DESC LIMIT 8""", args):
                    models.append({
                        "model": m[0] or "(unknown)", "sessions": m[1],
                        "input": m[2] or 0, "output": m[3] or 0,
                        "cache_read": m[4] or 0})

            denom = inp + cr + cw
            out.append({
                "name": name,
                "sessions": row[0],
                "api_calls": api_calls or row[0],
                "input": inp, "output": out_t,
                "cache_read": cr, "cache_write": cw,
                "reasoning": reasoning,
                "cost_usd": cost,
                "cost_anomaly": cost > COST_SANE_LIMIT,
                "actual_cost_usd": actual_cost,
                "messages": msgs, "tool_calls": tool_calls,
                "cache_hit_rate": round(cr / denom, 4) if denom else 0.0,
                "models": models,
            })
            con.close()
        except Exception as e:
            out.append({"name": name, "error": str(e)})
    return out


def _pure_input(sem, inp, cr, cc):
    """按 input_token_semantics 把原始 input 折算成「纯 input」。

    s=1 (total-inclusive) 的 input 已含 cache_read+cache_creation，须减去；
    s=0 legacy / s=2 fresh 的 input 本就不含 cache_read，原样返回。
    """
    if sem == 1:
        return max((inp or 0) - (cr or 0) - (cc or 0), 0)
    return inp or 0


def _rollup_day_cut(since_ts):
    """窗口起点折算成 usage_daily_rollups 的 date 过滤下界（本地日期）。

    rollups 只有 date 粒度（与 ccs_daily 的 localtime 口径一致），窗口起点
    当天整日计入；不过滤则窗口外历史同步量会灌进窗口视图。
    """
    if not since_ts:
        return None
    return time.strftime("%Y-%m-%d", time.localtime(since_ts))


def ccs_totals(since_ts=None):
    """CCS 总量。

    必须与 ccs_by_tool 口径一致（proxy 日志 + 会话同步 rollups 合并），
    否则顶部 KPI 会与「工具/Agent 消耗」表的合计对不上（实测踩过）。
    input 按 semantics 折算纯口径；成本含 proxy 日志（此前只取 rollups，
    Codex/Claude Desktop 走代理的成本全部漏计）。
    """
    if not os.path.exists(CCS_DB):
        return {}
    con = _ro(CCS_DB)
    cur = con.cursor()
    where, args = "", []
    if since_ts:
        where = "WHERE created_at >= ?"
        args = [int(since_ts)]
    # proxy 部分按语义分组取 token（latency 与语义无关，单独 AVG）
    p = {"req": 0, "input": 0, "output": 0, "cache_read": 0,
         "cache_creation": 0, "cost": 0.0}
    try:
        for r in cur.execute(f"""
                SELECT input_token_semantics, COUNT(*), SUM(input_tokens),
                       SUM(output_tokens), SUM(cache_read_tokens),
                       SUM(cache_creation_tokens)
                FROM proxy_request_logs {where}
                GROUP BY input_token_semantics""", args):
            p["req"] += r[1] or 0
            p["output"] += r[3] or 0
            p["cache_read"] += r[4] or 0
            p["cache_creation"] += r[5] or 0
            p["input"] += _pure_input(r[0], r[2], r[4], r[5])
        pc = _sum_row(cur, f"""
            SELECT SUM(CAST(total_cost_usd AS REAL))
            FROM proxy_request_logs {where}""", args)
        p["cost"] = (pc[0] or 0.0) if pc else 0.0   # 空表 SUM 返回 NULL
    except Exception:
        pass
    lat = _sum_row(cur, f"""
        SELECT COUNT(*), AVG(latency_ms), AVG(first_token_ms)
        FROM proxy_request_logs {where}""", args)
    succ = _sum_row(cur, f"""
        SELECT COUNT(*) FROM proxy_request_logs {where}
        {'AND' if where else 'WHERE'} status_code BETWEEN 200 AND 299""",
                    args)
    # 会话同步部分（CCS 从各客户端会话日志同步来的用量，带成本；全部标 2）
    # rollups 只有 date 粒度，窗口视图必须按下界日期过滤
    s = {"requests": 0, "input": 0, "output": 0, "cache_read": 0,
         "cache_creation": 0, "cost": 0.0}
    try:
        rwhere, rargs = "", []
        day_cut = _rollup_day_cut(since_ts)
        if day_cut:
            rwhere, rargs = "WHERE date >= ?", [day_cut]
        row = cur.execute(f"""
                SELECT SUM(request_count), SUM(input_tokens),
                       SUM(output_tokens), SUM(cache_read_tokens),
                       SUM(cache_creation_tokens),
                       SUM(CAST(total_cost_usd AS REAL))
                FROM usage_daily_rollups {rwhere}""", rargs).fetchone()
        if row:
            s = {"requests": row[0] or 0, "input": row[1] or 0,
                 "output": row[2] or 0, "cache_read": row[3] or 0,
                 "cache_creation": row[4] or 0, "cost": row[5] or 0.0}
    except Exception:
        pass
    by_app = []
    try:
        agg = {}
        for a in cur.execute(f"""
                SELECT app_type, input_token_semantics, COUNT(*),
                       SUM(input_tokens), SUM(output_tokens),
                       SUM(cache_read_tokens), SUM(cache_creation_tokens)
                FROM proxy_request_logs {where}
                GROUP BY app_type, input_token_semantics""", args):
            d = agg.setdefault(a[0], {"requests": 0, "input": 0,
                                      "output": 0, "cache_read": 0,
                                      "cache_creation": 0})
            d["requests"] += a[2] or 0
            d["output"] += a[4] or 0
            d["cache_read"] += a[5] or 0
            d["cache_creation"] += a[6] or 0
            d["input"] += _pure_input(a[1], a[3], a[5], a[6])
        for app, d in agg.items():
            denom = d["input"] + d["cache_read"] + d["cache_creation"]
            by_app.append({
                "app_type": app, "requests": d["requests"],
                "input": d["input"], "output": d["output"],
                "cache_read": d["cache_read"],
                "cache_creation": d["cache_creation"],
                "cache_hit_rate": round(d["cache_read"] / denom, 4)
                if denom else 0.0})
    except Exception:
        pass
    con.close()
    inp = p["input"] + s["input"]
    cr = p["cache_read"] + s["cache_read"]
    cc = p["cache_creation"] + s["cache_creation"]
    denom = inp + cr + cc
    return {
        "requests": p["req"] + s["requests"],
        "requests_proxy": p["req"],
        "requests_session": s["requests"],
        "success_requests": (succ[0] if succ else 0),
        "input": inp, "output": p["output"] + s["output"],
        "cache_read": cr, "cache_creation": cc,
        "cache_hit_rate": round(cr / denom, 4) if denom else 0.0,
        "cost_usd": round(p["cost"] + s["cost"], 4),
        "cost_usd_proxy": round(p["cost"], 4),
        "avg_latency_ms": round(lat[1] or 0) if lat else 0,
        "avg_first_token_ms": round(lat[2] or 0) if lat else 0,
        "by_app_type": by_app,
    }



def hermes_cc_usage(since_ts=None):
    """Hermes 直连 CC 适配器(3050)的用量——仪表盘同源数据（各 profile state.db）。

    优先读取 session_model_usage 中 billing_base_url LIKE '%3050%' 的分任务真实记账；
    兜底读取 sessions 表同名列。
    时间窗对齐最新活跃时刻，避免长会话被漏计。
    """
    out = {}
    pdir = os.path.join(HERMES, "profiles")
    dbs = []
    for name in _active_agents():
        p = _agent_db(name)
        if os.path.exists(p):
            dbs.append((name, p))
    if since_ts is None:
        since_ts = int(datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
    main_key = _hermes_cc_key(os.path.join(HERMES, ".env"))
    for name, db in dbs:
        try:
            key = main_key if name == "default" else (
                _hermes_cc_key(os.path.join(pdir, name, ".env")) or main_key)
            fp = (hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
                  if key else "(unknown)")
            con = _ro(db)
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            has_smu = "session_model_usage" in tables
            sess_cols = [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]
            has_act = "last_activity_at" in sess_cols

            if has_smu:
                where = "WHERE billing_base_url LIKE '%3050%'"
                args = []
                if since_ts:
                    where += " AND COALESCE(last_seen, 0) >= ?"
                    args.append(int(since_ts))
                row = _sum_row(cur, f"""
                    SELECT COUNT(DISTINCT session_id),
                           SUM(COALESCE(input_tokens,0)),
                           SUM(COALESCE(output_tokens,0)),
                           SUM(COALESCE(cache_read_tokens,0)),
                           MAX(COALESCE(last_seen, 0)),
                           SUM(COALESCE(api_call_count,0))
                    FROM session_model_usage {where}""", args)
            else:
                where = "WHERE billing_base_url LIKE '%3050%'"
                args = []
                if since_ts:
                    if has_act:
                        where += " AND MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0)) >= ?"
                    else:
                        where += " AND COALESCE(ended_at, started_at, 0) >= ?"
                    args.append(int(since_ts))
                row = _sum_row(cur, f"""
                    SELECT COUNT(*),
                           SUM(COALESCE(input_tokens,0)),
                           SUM(COALESCE(output_tokens,0)),
                           SUM(COALESCE(cache_read_tokens,0)),
                           MAX(COALESCE(last_activity_at, ended_at, started_at, 0)),
                           SUM(COALESCE(api_call_count,0))
                    FROM sessions {where}""", args)
            con.close()
            if row and row[0]:
                d = out.setdefault(fp, {"today_req": 0, "today_tokens": 0,
                                        "last_ts": 0, "profiles": []})
                # 「次」用 api_call_count（API 调用数），与 CCS 侧的逐请求口径对齐；
                # sessions 只是会话数（今天 23 个会话 ≈ 1500+ 次调用），语义差太远。
                d["today_req"] += (row[5] or row[0] or 0)
                d["today_tokens"] += (row[1] or 0) + (row[2] or 0) + (row[3] or 0)
                d["last_ts"] = max(d["last_ts"], row[4] or 0)
                d["profiles"].append(name)
        except Exception:
            continue
    return out


def _hermes_cc_key(env_path):
    """从 Hermes 的 .env 里取打 3050 用的 key（COMMANDCODE_API_KEY）。

    只回 user_ 形态的池 key；读不到或形态不对回 None。值绝不进日志/返回值，
    """
    try:
        with open(env_path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"^COMMANDCODE_API_KEY\s*=\s*(.+)$", txt, re.M)
    if not m:
        return None
    v = m.group(1).strip().strip('"').strip("'")
    return v if _CC_KEY_RE.fullmatch(v) else None


def ccs_models(since_ts=None, limit=20):
    if not os.path.exists(CCS_DB):
        return []
    con = _ro(CCS_DB)
    cur = con.cursor()
    where, args = "", []
    if since_ts:
        where = "WHERE created_at >= ?"
        args = [int(since_ts)]
    agg = {}
    try:
        for m in cur.execute(f"""
                SELECT model, input_token_semantics, COUNT(*),
                       SUM(input_tokens), SUM(output_tokens),
                       SUM(cache_read_tokens), SUM(cache_creation_tokens)
                FROM proxy_request_logs {where}
                GROUP BY model, input_token_semantics""", args):
            d = agg.setdefault(m[0], {"requests": 0, "input": 0,
                                      "output": 0, "cache_read": 0,
                                      "cache_creation": 0})
            d["requests"] += m[2] or 0
            d["output"] += m[4] or 0
            d["cache_read"] += m[5] or 0
            d["cache_creation"] += m[6] or 0
            d["input"] += _pure_input(m[1], m[3], m[5], m[6])
    except Exception:
        pass
    out = []
    for m, d in sorted(agg.items(),
                       key=lambda kv: -(kv[1]["input"] + kv[1]["output"]
                                        + kv[1]["cache_read"]))[:limit]:
        denom = (d["input"] + d["cache_read"] + d["cache_creation"])
        out.append({
            "model": m, "requests": d["requests"],
            "input": d["input"], "output": d["output"],
            "cache_read": d["cache_read"],
            "cache_creation": d["cache_creation"],
            "total_tokens": (d["input"] + d["output"] + d["cache_read"]
                             + d["cache_creation"]),
            "cache_hit_rate": round(d["cache_read"] / denom, 4)
            if denom else 0.0})
    con.close()
    return out


# ---------------------------------------------------------------- 模型来源选择
# 这个接口把所有来源投影成一致的模型行。统计页面的总量与合并图只能消费这份
# 归一化数据，避免顶部总量、图表分母和各来源小计走出三套算法。
MODEL_SOURCE_DEFINITIONS = (
    ("ccs", "CCS", "CC Switch 代理请求与会话同步汇总"),
    ("claude", "Claude 本机直读", "本机 ~/.claude/projects 逐条记账：CLI + 桌面端，已去重"),
    ("dsh", "DSH", "DeepSeek Harness 本机会话记账（~/.dsh/sessions）"),
    ("hermes", "Hermes", "本机 Hermes 各 Profile 会话库"),
    ("codex", "Codex", "本机 Codex rollout 用量"),
    ("zcode", "ZCode", "本机 ZCode model_usage 用量"),
    ("pi", "Pi / OMP", "本机 Pi 及 OMP assistant usage 事件"),
    ("opencode", "OpenCode", "本机 OpenCode assistant 消息用量"),
    ("commandcode", "CommandCode", "NEW-API 中路由到本机 3050 的已计费请求"),
    ("xiaomi_mimo", "Xiaomi MiMo 桌面端", "MiMo-Code 本机数据库"),
    ("workbuddy", "WorkBuddy", "本机 WorkBuddy trace 请求用量"),
    ("antigravity", "Antigravity", "本机 Antigravity 会话与 LS RPC 真实记账"),
)


def _model_bucket(models, name):
    key = str(name or "(unknown)").strip() or "(unknown)"
    return models.setdefault(key, {
        "model": key, "requests": 0, "sessions": 0, "input": 0,
        "output": 0, "reasoning": 0, "cache_read": 0,
        "cache_write": 0, "cache_creation": 0,
        "cache_denominator": 0, "total_tokens": 0,
    })


def _add_model_usage(models, name, *, requests=0, sessions=0, input=0,
                     output=0, reasoning=0, cache_read=0, cache_write=0,
                     cache_creation=0, cache_denominator=None,
                     total_tokens=None):
    row = _model_bucket(models, name)
    inp, out = int(input or 0), int(output or 0)
    reason = int(reasoning or 0)
    cache_r, cache_w = int(cache_read or 0), int(cache_write or 0)
    cache_c = int(cache_creation or 0)
    row["requests"] += int(requests or 0)
    row["sessions"] += int(sessions or 0)
    row["input"] += inp
    row["output"] += out
    row["reasoning"] += reason
    row["cache_read"] += cache_r
    row["cache_write"] += cache_w
    row["cache_creation"] += cache_c
    row["cache_denominator"] += int(
        cache_denominator if cache_denominator is not None
        else inp + cache_r + cache_w + cache_c)
    row["total_tokens"] += int(
        total_tokens if total_tokens is not None
        else inp + out + reason + cache_r + cache_w + cache_c)


def _source_result(source_id, models, *, available=True, reason="", note=""):
    definition = next(d for d in MODEL_SOURCE_DEFINITIONS if d[0] == source_id)
    rows = sorted(models.values(), key=lambda row: (-row["total_tokens"], row["model"]))
    has_data = any(row["requests"] or row["sessions"] or row["total_tokens"]
                   for row in rows)
    if available and not has_data and not reason:
        reason = "该时间范围内没有模型用量"
    return {
        "id": source_id, "name": definition[1], "available": bool(available),
        "has_data": bool(has_data), "reason": reason or None, "note": note,
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "requests": sum(row["requests"] for row in rows),
        "sessions": sum(row["sessions"] for row in rows), "models": rows,
    }


def _missing_source(source_id, reason="未发现本机数据"):
    return _source_result(source_id, {}, available=False, reason=reason)


def _source_number(obj, *keys):
    if not isinstance(obj, dict):
        return 0
    for key in keys:
        value = obj.get(key)
        if value is not None:
            try:
                return int(float(value))
            except (TypeError, ValueError, OverflowError):
                return 0
    return 0


def _usage_fields(usage):
    """读通用 usage 命名，不带入正文，也不让非法数值污染合计。"""
    if not isinstance(usage, dict):
        return {}
    cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
    return {
        "input": _source_number(usage, "input", "input_tokens", "inputTokens",
                                "totalInputTokens"),
        "output": _source_number(usage, "output", "output_tokens", "outputTokens",
                                 "totalOutputTokens"),
        "reasoning": _source_number(usage, "reasoning", "reasoning_tokens",
                                    "reasoningTokens"),
        "cache_read": _source_number(usage, "cache_read", "cache_read_tokens",
                                     "cacheRead", "cacheReadTokens", "cached_input_tokens",
                                     "totalCachedTokens") or _source_number(
                                         cache, "read", "cache_read", "cacheRead"),
        "cache_write": _source_number(usage, "cache_write", "cache_write_tokens",
                                      "cacheWrite", "cacheWriteTokens") or _source_number(
                                          cache, "write", "cache_write", "cacheWrite"),
        "total_tokens": _source_number(usage, "total_tokens", "totalTokens",
                                        "computed_total_tokens"),
    }


def _timestamp_epoch(value):
    if isinstance(value, dict):
        for key in ("created", "timestamp", "updated", "started", "start"):
            if value.get(key) is not None:
                parsed = _timestamp_epoch(value[key])
                if parsed:
                    return parsed
        return None
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 100000000000:
            number /= 1000.0
        return number if number > 0 else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith("{"):
            try:
                nested = json.loads(raw)
                if isinstance(nested, dict):
                    return _timestamp_epoch(nested)
            except ValueError:
                pass
        try:
            number = float(raw)
            return _timestamp_epoch(number)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def _record_timestamp(*values, fallback=None):
    for value in values:
        parsed = _timestamp_epoch(value)
        if parsed:
            return parsed
    return fallback


def _in_window(timestamp, since_ts):
    return not since_ts or (timestamp is not None and timestamp >= since_ts)


def ccs_models_full(since_ts=None):
    """完整 CCS 模型用量：代理请求 + 会话同步 rollups，与 ccs_totals 同口径。

    剔除 claude / claude-desktop 的会话同步行（2026-10-08）：同一批会话已由
    本机 jsonl 直读（来源 claude），留着会双算；claude-desktop 走代理的聊天行
    不落 jsonl，仍留在本来源。
    """
    if not os.path.exists(CCS_DB):
        return _missing_source("ccs")
    con = _ro(CCS_DB)
    models = {}
    try:
        cur = con.cursor()
        where, args = ("WHERE created_at >= ?", [int(since_ts)]) if since_ts else ("", [])
        for row in cur.execute(f"""
                SELECT model, input_token_semantics, COUNT(*), SUM(input_tokens),
                       SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_creation_tokens)
                FROM proxy_request_logs {where}
                {"AND" if where else "WHERE"} NOT (app_type = 'claude'
                     OR (app_type = 'claude-desktop'
                         AND data_source = 'session_log'))
                GROUP BY model, input_token_semantics""", args):
            inp = _pure_input(row[1], row[3], row[5], row[6])
            cr, cc = int(row[5] or 0), int(row[6] or 0)
            _add_model_usage(models, row[0], requests=row[2], input=inp,
                             output=row[4], cache_read=cr, cache_creation=cc,
                             cache_denominator=inp + cr + cc)

        rollup_where, rollup_args = "", []
        day_cut = _rollup_day_cut(since_ts)
        if day_cut:
            rollup_where, rollup_args = "WHERE date >= ?", [day_cut]
        for row in cur.execute(f"""
                SELECT model, app_type, SUM(request_count), SUM(input_tokens),
                       SUM(output_tokens), SUM(cache_read_tokens),
                       SUM(cache_creation_tokens)
                FROM usage_daily_rollups {rollup_where}
                GROUP BY model, app_type""", rollup_args):
            if row[1] in ("claude", "claude-desktop"):
                continue
            inp, cr, cc = int(row[3] or 0), int(row[5] or 0), int(row[6] or 0)
            _add_model_usage(models, row[0], requests=row[2], input=inp,
                             output=row[4], cache_read=cr, cache_creation=cc,
                             cache_denominator=inp + cr + cc)
    finally:
        con.close()
    return _source_result(
        "ccs", models,
        note="input 按 input_token_semantics 折算；会话同步 rollups 按日筛选；"
             "claude 会话同步行归入「Claude 本机直读」来源，不在本来源内。")


def hermes_models_full(since_ts=None):
    """逐个 profile 聚合全量会话模型，绕过 hermes_agents 的 Top 8 展示限制。
    优先读取 session_model_usage 获取真实的逐任务/逐模型明细与 API 调用次数；
    时间窗对齐最新活跃时刻。
    """
    models = {}
    found_db = False
    for name in _active_agents():
        db = _agent_db(name)
        if not os.path.exists(db):
            continue
        found_db = True
        con = _ro(db)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            has_smu = "session_model_usage" in tables
            sess_cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()]
            has_act = "last_activity_at" in sess_cols

            if has_smu:
                smu_where, smu_args = "", []
                if since_ts:
                    if has_act:
                        smu_where = """WHERE session_id IN (
                            SELECT id FROM sessions
                            WHERE MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0)) >= ?
                        )"""
                    else:
                        smu_where = """WHERE session_id IN (
                            SELECT id FROM sessions
                            WHERE COALESCE(ended_at, started_at, 0) >= ?
                        )"""
                    smu_args = [int(since_ts)]
                for row in con.execute(f"""
                        SELECT model, COUNT(DISTINCT session_id),
                               SUM(COALESCE(input_tokens,0)),
                               SUM(COALESCE(output_tokens,0)),
                               SUM(COALESCE(cache_read_tokens,0)),
                               SUM(COALESCE(cache_write_tokens,0)),
                               SUM(COALESCE(api_call_count,0)),
                               SUM(COALESCE(reasoning_tokens,0))
                        FROM session_model_usage {smu_where}
                        GROUP BY model""", smu_args):
                    inp, cr, cw = int(row[2] or 0), int(row[4] or 0), int(row[5] or 0)
                    _add_model_usage(models, row[0], sessions=row[1],
                                     requests=int(row[6] or 0),
                                     input=inp, output=row[3],
                                     reasoning=int(row[7] or 0),
                                     cache_read=cr, cache_write=cw,
                                     cache_denominator=inp + cr + cw)
            else:
                where, args = ("", [])
                if since_ts:
                    if has_act:
                        where = "WHERE MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0)) >= ?"
                    else:
                        where = "WHERE COALESCE(ended_at, started_at, 0) >= ?"
                    args = [int(since_ts)]
                for row in con.execute(f"""
                        SELECT model, COUNT(*), SUM(COALESCE(input_tokens,0)),
                               SUM(COALESCE(output_tokens,0)),
                               SUM(COALESCE(cache_read_tokens,0)),
                               SUM(COALESCE(cache_write_tokens,0))
                        FROM sessions {where} GROUP BY model""", args):
                    inp, cr, cw = int(row[2] or 0), int(row[4] or 0), int(row[5] or 0)
                    _add_model_usage(models, row[0], sessions=row[1], input=inp,
                                     output=row[3], cache_read=cr, cache_write=cw,
                                     cache_denominator=inp + cr + cw)
        finally:
            con.close()
    if not found_db:
        return _missing_source("hermes")
    return _source_result("hermes", models,
                          note="Hermes session 的 reasoning 属于 output 子集，不另行重复累加。")


def _legacy_model_source(source_id, tool, *, note=""):
    if not tool or tool.get("available") is False:
        return _missing_source(source_id, (tool or {}).get("reason") or "未发现本机用量文件")
    models = {}
    for model in tool.get("models") or []:
        inp = int(model.get("input") or 0)
        out = int(model.get("output") or 0)
        reason = int(model.get("reasoning") or 0)
        cr = int(model.get("cache_read") or 0)
        cw = int(model.get("cache_write") or 0)
        cc = int(model.get("cache_creation") or 0)
        request_count = int(model.get("requests") or 0)
        session_count = int(model.get("sessions") or 0)
        total = model.get("total_tokens", model.get("total"))
        if source_id == "zcode" and tool.get("source") == "zcode:transcript":
            total = None
        if total is None:
            total = inp + out + reason + cr + cw + cc
        if source_id in ("codex", "zcode"):
            cache_denominator = inp + cr if cr > inp else inp
        else:
            cache_denominator = inp + cr + cw + cc
        _add_model_usage(
            models, model.get("model"), requests=request_count,
            sessions=session_count, input=inp, output=out, reasoning=reason,
            cache_read=cr, cache_write=cw, cache_creation=cc,
            cache_denominator=model.get("cache_denominator", cache_denominator),
            total_tokens=total)
    return _source_result(source_id, models, note=note or tool.get("note", ""))


def codex_model_source(since_ts=None):
    return _legacy_model_source(
        "codex", codex_stats(since_ts),
        note="rollout 按逐响应事件时间戳把文件累计分摊进时间窗，长寿会话仅窗口内增量计入当前窗口；input 已含缓存时不重复累加，少量 cache_read > input 的上游按分离缓存计。")


def zcode_model_source(since_ts=None):
    tool = zcode_stats(since_ts)
    note = "ZCode 的 input 已含 cache_read；总量使用其权威 computed_total_tokens。"
    if tool.get("source") == "zcode:transcript":
        note += " 当前使用 transcript 兜底，有限时间窗只统计带时间戳的记录。"
    return _legacy_model_source("zcode", tool, note=note)


def antigravity_model_source(since_ts=None):
    tool = antigravity_tokens(since=since_ts)
    note = "Antigravity 本地 LS RPC + 会话持久化缓存记账；有限时间窗按会话活动时间过滤。"
    return _legacy_model_source("antigravity", tool, note=note)


PI_SESSION_DIRS = [
    os.path.expanduser(os.path.join("~", ".pi", "agent", "sessions")),
    os.path.expanduser(os.path.join("~", ".omp", "agent", "sessions")),
]
PI_SESSION_DIR = PI_SESSION_DIRS[0]


def _pi_session_dirs():
    dirs = list(PI_SESSION_DIRS)
    env_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if env_dir:
        dirs.extend([os.path.join(env_dir, "sessions"), env_dir])
    return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]


def _pi_session_paths():
    paths = []
    for d in _pi_session_dirs():
        paths.extend(glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True))
    return list(dict.fromkeys(paths))


WORKBUDDY_TRACE_DIR = os.path.expanduser(os.path.join("~", ".workbuddy", "traces"))


def _sqlite_data_rows(path):
    """只读提取 message.data 中的角色、模型、usage 与时间 JSON 字段。

    不把完整 data JSON 读入 Python，因此正文/内容字段不会被采集器加载。
    """
    con = _ro(path)
    try:
        columns = {row[1] for row in con.execute('PRAGMA table_info("message")')}
        data_col = next((name for name in ("data", "data_json", "payload")
                         if name in columns), None)
        if not data_col:
            raise sqlite3.OperationalError("message 表缺少 data 列")
        time_col = next((name for name in ("time_created", "created_at", "timestamp")
                         if name in columns), None)
        time_expr = '"' + time_col.replace('"', '""') + '"' if time_col else "NULL"
        data_expr = '"' + data_col.replace('"', '""') + '"'
        def field(path):
            return f"CASE WHEN json_valid({data_expr}) THEN json_extract({data_expr}, '{path}') END"
        role_expr = f"COALESCE({field('$.info.role')}, {field('$.role')})"
        model_expr = f"COALESCE({field('$.info.modelID')}, {field('$.modelID')}, {field('$.info.model')}, {field('$.model')})"
        provider_expr = f"COALESCE({field('$.info.providerID')}, {field('$.providerID')})"
        tokens_expr = f"COALESCE({field('$.info.tokens')}, {field('$.tokens')})"
        usage_expr = f"COALESCE({field('$.info.usage')}, {field('$.usage')})"
        message_time_expr = f"COALESCE({field('$.info.time')}, {field('$.time')})"
        timestamp_expr = f"COALESCE({field('$.info.timestamp')}, {field('$.timestamp')})"
        return con.execute(
            f"SELECT {time_expr}, {role_expr}, {model_expr}, {tokens_expr}, "
            f"{usage_expr}, {message_time_expr}, {timestamp_expr}, {provider_expr} FROM message").fetchall()
    finally:
        con.close()


def _json_object(raw):
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pi_model_source(since_ts=None):
    paths = _pi_session_paths()
    if not paths and not _pi_session_dirs():
        return _missing_source("pi", "未发现 Pi / OMP session 目录")
    models = {}
    file_count = opened_count = read_errors = 0
    for path in paths:
        file_count += 1
        try:
            if since_ts and os.path.getmtime(path) < since_ts:
                opened_count += 1
                continue
            fh = open(path, encoding="utf-8", errors="replace")
            opened_count += 1
        except OSError:
            read_errors += 1
            continue
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError):
                    read_errors += 1
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                if kind == "message":
                    if message.get("role") != "assistant":
                        continue
                    usage = message.get("usage") or entry.get("usage")
                elif kind in ("usage", "compaction", "branch_summary"):
                    usage = entry.get("usage") or entry.get("tokens") or message.get("usage")
                else:
                    continue
                if not isinstance(usage, dict):
                    continue
                ts = _record_timestamp(
                    message.get("timestamp"), entry.get("timestamp"),
                    message.get("time"), entry.get("time"))
                if not _in_window(ts, since_ts):
                    continue
                values = _usage_fields(usage)
                reported_total = values.pop("total_tokens", 0)
                model = (message.get("model") or message.get("modelID")
                         or entry.get("model") or entry.get("modelID") or "(unknown)")
                _add_model_usage(
                    models, model, requests=1, **values,
                    total_tokens=reported_total or None)
    if file_count and not opened_count:
        return _missing_source("pi", "Pi / OMP session 文件无法读取")
    return _source_result(
        "pi", models, available=not (file_count > 0 and read_errors > 0 and not models),
        reason=(f"有 {read_errors} 条记录无法读取" if read_errors else ""),
        note="读取 Pi 及 OMP 本机会话（~/.pi 与 ~/.omp）assistant usage、compaction 等用量字段；有限时间窗按消息时间过滤。")


def _opencode_db_candidates():
    candidates = []
    appdata = os.environ.get("APPDATA")
    xdg = os.environ.get("XDG_DATA_HOME")
    if appdata:
        candidates.append(os.path.join(appdata, "opencode", "opencode.db"))
    if xdg:
        candidates.append(os.path.join(xdg, "opencode", "opencode.db"))
    candidates.append(os.path.expanduser(os.path.join("~", ".local", "share", "opencode", "opencode.db")))
    return list(dict.fromkeys(candidates))


def opencode_model_source(since_ts=None):
    path = next((p for p in _opencode_db_candidates() if os.path.isfile(p)), None)
    if not path:
        return _missing_source("opencode", "未发现 OpenCode 本机数据库")
    models = {}
    rows = _sqlite_data_rows(path)
    for raw_time, role, name, raw_tokens, raw_usage, message_time, timestamp, provider in rows:
        if role != "assistant":
            continue
        tokens = _json_object(raw_tokens) or _json_object(raw_usage)
        if not tokens:
            continue
        ts = _record_timestamp(message_time, raw_time, timestamp)
        if not _in_window(ts, since_ts):
            continue
        values = _usage_fields(tokens)
        values.pop("total_tokens", None)  # OpenCode stats 按官方分项求和
        display_name = f"{provider}/{name}" if provider and name else (name or "(unknown)")
        _add_model_usage(models, display_name, requests=1, **values)
    return _source_result("opencode", models,
                          note="assistant message 总量按 input + output + reasoning + cache read/write 汇总；有限时间窗按消息时间过滤。")


def _mimocode_db_candidates():
    candidates = [os.path.expanduser(os.path.join(
        "~", ".local", "share", "mimocode", "mimocode.db"))]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.extend((
            os.path.join(appdata, "Xiaomi MiMo", "mimocode", "mimocode.db"),
            os.path.join(appdata, "Xiaomi MiMo", "mimocode.db"),
        ))
    return list(dict.fromkeys(candidates))


def xiaomi_mimo_model_source(since_ts=None):
    path = next((p for p in _mimocode_db_candidates() if os.path.isfile(p)), None)
    if not path:
        return _missing_source("xiaomi_mimo", "未发现 Xiaomi MiMo 桌面端 mimocode.db")
    models = {}
    for raw_time, role, name, raw_tokens, raw_usage, message_time, timestamp, provider in _sqlite_data_rows(path):
        if role != "assistant":
            continue
        tokens = _json_object(raw_tokens) or _json_object(raw_usage)
        if not tokens:
            continue
        ts = _record_timestamp(message_time, raw_time, timestamp)
        if not _in_window(ts, since_ts):
            continue
        values = _usage_fields(tokens)
        values.pop("total_tokens", None)
        display_name = f"{provider}/{name}" if provider and name else (name or "(unknown)")
        _add_model_usage(models, display_name,
                         requests=1, **values)
    return _source_result(
        "xiaomi_mimo", models,
        note="直接读取 Xiaomi MiMo 桌面端共享的 MiMo-Code 数据库，按官方 stats.ts 口径求和。有限时间窗按消息时间过滤。")


def _workbuddy_trace_models(trace):
    info = trace.get("modelInfo") if isinstance(trace.get("modelInfo"), dict) else {}
    root_usage = {
        "input": _source_number(info, "totalInputTokens", "inputTokens", "input_tokens"),
        "output": _source_number(info, "totalOutputTokens", "outputTokens", "output_tokens"),
        "cache_read": _source_number(info, "totalCachedTokens", "cachedTokens", "cacheRead", "cacheReadTokens"),
        "total_tokens": _source_number(trace, "totalTokens", "total_tokens"),
    }
    raw_models = info.get("models")
    entries = []
    if isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, dict):
                entries.append((item.get("model") or item.get("modelName") or item.get("name"),
                                item.get("usage") or item.get("tokens") or item))
            elif isinstance(item, str):
                entries.append((item, {}))
    elif isinstance(raw_models, dict):
        if any(k in raw_models for k in (
                "model", "modelName", "name", "inputTokens", "totalInputTokens",
                "totalTokens", "usage", "tokens")):
            entries.append((raw_models.get("model") or raw_models.get("modelName"),
                            raw_models.get("usage") or raw_models.get("tokens") or raw_models))
        else:
            entries.extend((name, value if isinstance(value, dict) else {})
                           for name, value in raw_models.items())
    if not entries:
        entries = [(info.get("model") or info.get("modelName") or trace.get("model") or "(unknown)", info)]

    # 某些版本只记录 modelInfo.models 的名称，token 明细仍在 modelInfo 根节点。
    model_rows = []
    for name, usage in entries:
        fields = _usage_fields(usage)
        model_rows.append((name or "(unknown)", fields))
    has_model_usage = any(any(v.get(k) for k in (
        "input", "output", "reasoning", "cache_read", "cache_write", "total_tokens"))
        for _, v in model_rows)
    if not has_model_usage:
        if len(model_rows) > 1:
            model_rows = [("(multiple models)", root_usage)]
        else:
            model_rows = [(model_rows[0][0], root_usage)]
    elif len(model_rows) == 1 and not model_rows[0][1].get("total_tokens"):
        fields = model_rows[0][1]
        fields["total_tokens"] = root_usage["total_tokens"] or (
            fields.get("input", 0) + fields.get("output", 0)
            + fields.get("reasoning", 0) + fields.get("cache_read", 0)
            + fields.get("cache_write", 0))
    return model_rows


def workbuddy_model_source(since_ts=None):
    if not os.path.isdir(WORKBUDDY_TRACE_DIR):
        return _missing_source("workbuddy", "未发现 WorkBuddy traces 目录")
    paths = glob.glob(os.path.join(WORKBUDDY_TRACE_DIR, "**", "trace_*.json"), recursive=True)
    models = {}
    read_errors = read_files = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                trace = json.load(fh)
        except (OSError, ValueError):
            read_errors += 1
            continue
        read_files += 1
        if not isinstance(trace, dict):
            continue
        timestamp = _record_timestamp(
            trace.get("timestamp"), trace.get("createdAt"), trace.get("created_at"),
            trace.get("startTime"), trace.get("time"))
        if not _in_window(timestamp, since_ts):
            continue
        for name, values in _workbuddy_trace_models(trace):
            total = values.get("total_tokens") or None
            _add_model_usage(models, name, requests=1,
                             input=values.get("input"), output=values.get("output"),
                             reasoning=values.get("reasoning"),
                             cache_read=values.get("cache_read"),
                             cache_write=values.get("cache_write"),
                             total_tokens=total)
    if paths and not read_files:
        return _missing_source("workbuddy", "WorkBuddy trace 文件无法读取")
    return _source_result(
        "workbuddy", models, available=not (paths and read_errors and not models),
        reason=(f"有 {read_errors} 个 trace 文件无法读取" if read_errors else ""),
        note="用量来自 ~/.workbuddy/traces/**/trace_*.json；session_usage.used 是上下文状态，不作为累计用量。有限时间窗按 trace 时间过滤，无时间戳文件只计入全部历史。")



def model_sources_overview(since=None):
    """返回统一模型来源状态、模型明细与来源小计。

    since：窗口起点（秒级时间戳，可选）。?start= 自定义范围由路由层传入；
    传 None 统计全部历史。
    """
    collectors = (
        ("ccs", lambda: ccs_models_full(since)),
        ("claude", lambda: claude_model_source(since)),
        ("dsh", lambda: dsh_model_source(since)),
        ("hermes", lambda: hermes_models_full(since)),
        ("codex", lambda: codex_model_source(since)),
        ("zcode", lambda: zcode_model_source(since)),
        ("pi", lambda: pi_model_source(since)),
        ("opencode", lambda: opencode_model_source(since)),
        ("xiaomi_mimo", lambda: xiaomi_mimo_model_source(since)),
        ("workbuddy", lambda: workbuddy_model_source(since)),
        ("antigravity", lambda: antigravity_model_source(since)),
    )
    sources = []
    for source_id, collector in collectors:
        try:
            sources.append(collector())
        except Exception:
            # 数据库 schema 更新、文件临时占用等只影响单个来源；不返回路径、SQL 或正文。
            sources.append(_missing_source(source_id, "读取失败；其他来源仍可统计"))
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "since": since, "sources": sources,
        "note": "总量由模型明细求和；跨来源不去重，可能包含同一请求。",
    }


def ccs_daily(limit=60):
    """按日趋势。

    实测：`usage_daily_rollups` 只有零星几行（6 行），拿不到连续趋势；
    真正的日粒度数据在 1.2 万条逐请求日志里 → 改为从 `proxy_request_logs`
    按本地日期聚合 token/请求数，成本按同日从 rollups 补齐。
    """
    if not os.path.exists(CCS_DB):
        return []
    con = _ro(CCS_DB)
    cur = con.cursor()
    rows = {}
    try:
        # 按天+语义分组，s=1 的 input 折算纯口径后再并天
        for r in cur.execute("""
                SELECT date(created_at, 'unixepoch', 'localtime') AS d,
                       input_token_semantics, COUNT(*),
                       SUM(input_tokens), SUM(output_tokens),
                       SUM(cache_read_tokens), SUM(cache_creation_tokens)
                FROM proxy_request_logs GROUP BY d, input_token_semantics"""):
            e = rows.setdefault(r[0], {
                "date": r[0], "requests": 0, "success": 0, "input": 0,
                "output": 0, "cache_read": 0, "cache_creation": 0,
                "cost_usd": 0.0})
            e["requests"] += r[2] or 0
            e["output"] += r[4] or 0
            e["cache_read"] += r[5] or 0
            e["cache_creation"] += r[6] or 0
            e["input"] += _pure_input(r[1], r[3], r[5], r[6])
        cost = {}
        try:
            for c in cur.execute("""
                    SELECT date, SUM(CAST(total_cost_usd AS REAL))
                    FROM usage_daily_rollups GROUP BY date"""):
                cost[c[0]] = c[1] or 0.0
            for c in cur.execute("""
                    SELECT date(created_at,'unixepoch','localtime') d,
                           SUM(CAST(total_cost_usd AS REAL))
                    FROM proxy_request_logs GROUP BY d"""):
                cost[c[0]] = cost.get(c[0], 0.0) + (c[1] or 0.0)
        except Exception:
            pass
        succ = {}
        try:
            for s in cur.execute("""
                    SELECT date(created_at,'unixepoch','localtime') d, COUNT(*)
                    FROM proxy_request_logs
                    WHERE status_code BETWEEN 200 AND 299 GROUP BY d"""):
                succ[s[0]] = s[1] or 0
        except Exception:
            pass
        for e in rows.values():
            e["cost_usd"] = round(cost.get(e["date"], 0.0), 4)
            e["success"] = succ.get(e["date"], 0)
    except Exception:
        pass
    con.close()
    out = sorted(rows.values(), key=lambda e: e["date"])
    return out[-limit:]


def summary(since=None):
    """since：窗口起点（秒级时间戳，可选），None=全部历史。"""
    h = hermes_agents(since)
    c = ccs_totals(since)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "since": since,
        "hermes": {
            "agents": len([a for a in h if "error" not in a]),
            "sessions": sum(a.get("sessions", 0) for a in h),
            "input": sum(a.get("input", 0) for a in h),
            "output": sum(a.get("output", 0) for a in h),
            "cache_read": sum(a.get("cache_read", 0) for a in h),
            "cache_write": sum(a.get("cache_write", 0) for a in h),
            "reasoning": sum(a.get("reasoning", 0) for a in h),
            "cost_usd": round(sum(a.get("cost_usd", 0) for a in h
                                  if not a.get("cost_anomaly")), 4),
            "cost_anomaly_agents": [a["name"] for a in h
                                    if a.get("cost_anomaly")],
            # 被剔除的异常金额（给前端显示唯一口径的数字，避免两处不一致）
            "cost_anomaly_usd": round(sum(a.get("cost_usd", 0) for a in h
                                          if a.get("cost_anomaly")), 2),
        },
        "ccs": c,
    }


# ---------------------------------------------------------------- 工具维度
ZCODE_AGENTS = os.path.expanduser(r"~\.zcode\cli\agents")
ZCODE_DB = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
ANTIGRAVITY_DB = os.path.expanduser(
    r"~\.gemini\antigravity\conversation_summaries.db")
# Antigravity 语言服务器端点缓存（端口/令牌每次启动都变）
_LS_CACHE = {"ts": 0, "port": None, "token": None}

# CCS app_type -> 工具名（用户口径）
CCS_TOOL_LABELS = {
    "claude": "Claude Code (CC)",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "gemini": "Gemini CLI",
    "grokbuild": "Grok Build",
}


def zcode_stats(since_ts=None):
    """ZCode（Z.ai 的编码 agent）用量。

    **权威数据源**：`~/.zcode/cli/db/db.sqlite` → `model_usage` 表
    （每次模型调用一行：model_id / agent / query_source / status / 耗时 /
    input_tokens / output_tokens / cache_read_input_tokens / computed_total_tokens）。

    实测教训：早期版本解析 `agents/*/transcript.jsonl`，只覆盖到 2/5 会话、
    197 条调用，而真实是 **2,963 条 / 44 会话**——少算约 25 倍。transcript
    仅作 DB 缺失时的兜底。

    口径差异（重要）：ZCode 的 `input_tokens` **已包含** cache_read
    （OpenAI 风格，不同于 CCS/Hermes 的 Anthropic 口径）→
    总 token = input + output；命中率 = cache_read / input。
    """
    if os.path.exists(ZCODE_DB):
        return _zcode_from_db(since_ts)
    return _zcode_from_transcripts(since_ts)


def _zcode_from_db(since_ts=None):
    """从 model_usage 表聚合（权威口径）。"""
    con = _ro(ZCODE_DB)
    cur = con.cursor()
    where, args = "", []
    if since_ts:
        where = "WHERE started_at >= ?"
        args = [int(since_ts * 1000)]
    r = cur.execute(f"""
        SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
               SUM(cache_read_input_tokens), SUM(cache_creation_input_tokens),
               SUM(computed_total_tokens), COUNT(DISTINCT session_id),
               AVG(duration_ms), AVG(time_to_first_token_ms)
        FROM model_usage {where}""", args).fetchone()
    req, inp, outp, cr, cw, tot, sess, dur, ttft = r
    inp = inp or 0
    models = []
    try:
        for m in cur.execute(f"""
                SELECT model_id, COUNT(*), SUM(input_tokens),
                       SUM(output_tokens), SUM(cache_read_input_tokens),
                       SUM(computed_total_tokens)
                FROM model_usage {where} GROUP BY model_id
                ORDER BY 6 DESC""", args):
            models.append({
                "model": m[0], "requests": m[1], "input": m[2] or 0,
                "output": m[3] or 0, "cache_read": m[4] or 0,
                "total": m[5] or 0,
                "cache_hit_rate": round((m[4] or 0) / (m[2] or 1), 4),
            })
    except Exception:
        pass
    agents = []
    try:
        for a in cur.execute(f"""
                SELECT agent, COUNT(*), SUM(computed_total_tokens)
                FROM model_usage {where} GROUP BY agent ORDER BY 3 DESC""",
                             args):
            agents.append({"agent": a[0] or "(unknown)", "requests": a[1],
                           "total_tokens": a[2] or 0})
    except Exception:
        pass
    status = {}
    try:
        for s in cur.execute(f"SELECT status, COUNT(*) FROM model_usage "
                             f"{where} GROUP BY status", args):
            status[s[0]] = s[1]
    except Exception:
        pass
    con.close()
    return {
        "name": "ZCode", "available": True, "source": "zcode:model_usage",
        "sessions": sess or 0, "requests": req or 0,
        "input": inp, "output": outp or 0,
        "cache_read": cr or 0, "cache_write": cw or 0,
        # input 已含 cache_read → 总量 = input + output（勿再加 cache）
        "total_tokens": inp + (outp or 0),
        "provider_total_tokens": tot or 0,
        "cache_hit_rate": round((cr or 0) / inp, 4) if inp else 0.0,
        "cost_usd": None,
        "avg_duration_ms": round(dur or 0),
        "avg_ttft_ms": round(ttft or 0),
        "status_counts": status,
        "models": models, "agents": agents,
        "note": "input 含 cache_read（OpenAI 口径）；命中率=cache_read/input",
    }


def _zcode_from_transcripts(since_ts=None):
    """兜底：从 transcript.jsonl 解析（数据不全，仅 db 缺失时用）。"""
    if not os.path.isdir(ZCODE_AGENTS):
        return {"name": "ZCode", "available": False,
                "reason": "ZCode 数据目录不存在"}
    files = glob.glob(os.path.join(
        ZCODE_AGENTS, "sess_*", "agent_*", "transcript.jsonl"))
    turn_model = {}
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
           "total": 0, "requests": 0}
    models = {}
    sessions = set()
    for p in files:
        sess = os.path.basename(os.path.dirname(os.path.dirname(p)))
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for line in fh:
                if "token" not in line.lower():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if since_ts and not _in_window(
                        _record_timestamp(d.get("timestamp"), d.get("createdAt"),
                                          d.get("created_at")), since_ts):
                    continue
                t = d.get("type")
                tid = d.get("turnId")
                pl = d.get("payload") or {}
                if t in ("model_request", "model_network_status"):
                    m = pl.get("model")
                    name = None
                    if isinstance(m, dict):
                        name = m.get("modelId") or m.get("model")
                    elif isinstance(m, str):
                        name = m.split("/")[-1]
                    if name and tid:
                        turn_model[tid] = name
                elif t == "model_complete":
                    u = pl.get("usage") or {}
                    if not u:
                        continue
                    sessions.add(sess)
                    tot["requests"] += 1
                    for k_src, k_dst in (("inputTokens", "input"),
                                         ("outputTokens", "output"),
                                         ("cacheReadTokens", "cache_read"),
                                         ("cacheWriteTokens", "cache_write"),
                                         ("totalTokens", "total")):
                        tot[k_dst] += u.get(k_src, 0) or 0
                    key = turn_model.get(tid, "(unknown)")
                    e = models.setdefault(key, {
                        "model": key, "requests": 0, "input": 0, "output": 0,
                        "cache_read": 0, "cache_write": 0, "total": 0})
                    e["requests"] += 1
                    e["input"] += u.get("inputTokens", 0) or 0
                    e["output"] += u.get("outputTokens", 0) or 0
                    e["cache_read"] += u.get("cacheReadTokens", 0) or 0
                    e["cache_write"] += u.get("cacheWriteTokens", 0) or 0
                    e["total"] += u.get("totalTokens", 0) or 0
    denom = tot["input"] + tot["cache_read"] + tot["cache_write"]
    for e in models.values():
        d2 = e["input"] + e["cache_read"]
        e["cache_hit_rate"] = round(e["cache_read"] / d2, 4) if d2 else 0.0
    return {
        "name": "ZCode", "available": True, "source": "zcode:transcript",
        "sessions": len(sessions), "requests": tot["requests"],
        "input": tot["input"], "output": tot["output"],
        "cache_read": tot["cache_read"], "cache_write": tot["cache_write"],
        # 口径统一：所有源都用 input+output+cache_read+cache_write
        "total_tokens": (tot["input"] + tot["output"] + tot["cache_read"]
                         + tot["cache_write"]),
        # 记录自带的 totalTokens（=input+output，不含 cache）留档备查
        "record_total_tokens": tot["total"],
        "cache_hit_rate": round(tot["cache_read"] / denom, 4) if denom else 0.0,
        "cost_usd": None,
        "models": sorted(models.values(), key=lambda x: -x["total"]),
    }


def ccs_by_tool(since_ts=None):
    """CCS 侧按 app_type 拆分的各 CLI/客户端用量。

    两个来源要合并（否则 CC 会严重少算）：
      - `proxy_request_logs`：走 CCS 代理的实时请求（claude-desktop / codex / claude）
      - `usage_daily_rollups`：CCS 从客户端会话日志同步来的用量（provider_id
        形如 `_session` / `_codex_session`，**带成本**，CC 的用量主要在这里）
    """
    if not os.path.exists(CCS_DB):
        return []
    con = _ro(CCS_DB)
    cur = con.cursor()
    where, args = "", []
    if since_ts:
        where = "WHERE created_at >= ?"
        args = [int(since_ts)]
    # 会话同步部分（按 app_type 聚合；rollups 全标 2，input 已是纯口径；
    # 窗口视图按 date 下界过滤，与 ccs_totals 同约定）。
    # claude / claude-desktop 的同步量已改由本机 jsonl 直读（claude_native_stats），
    # 这里剔除防双算（2026-10-02）。
    sess = {}
    try:
        rwhere, rargs = "", []
        day_cut = _rollup_day_cut(since_ts)
        if day_cut:
            rwhere, rargs = "WHERE date >= ?", [day_cut]
        for r in cur.execute(f"""
                SELECT app_type,
                       SUM(request_count), SUM(input_tokens),
                       SUM(output_tokens), SUM(cache_read_tokens),
                       SUM(cache_creation_tokens),
                       SUM(CAST(total_cost_usd AS REAL))
                FROM usage_daily_rollups {rwhere} GROUP BY app_type""", rargs):
            if r[0] in ("claude", "claude-desktop"):
                continue
            sess[r[0]] = {
                "requests": r[1] or 0, "input": r[2] or 0, "output": r[3] or 0,
                "cache_read": r[4] or 0, "cache_creation": r[5] or 0,
                "cost": r[6] or 0.0}
    except Exception:
        pass
    # proxy 部分：按 (app_type, semantics) 分组取 token，input 折算纯口径。
    # claude 的同步行（data_source='session_log'）与走代理的 claude 行均已由
    # 本机 jsonl 覆盖，剔除；claude-desktop 的 proxy 行保留（桌面端聊天走代理
    # 不落 jsonl），其同步行剔除。
    agg = {}
    try:
        for r in cur.execute(f"""
                SELECT app_type, input_token_semantics, COUNT(*),
                       SUM(input_tokens), SUM(output_tokens),
                       SUM(cache_read_tokens), SUM(cache_creation_tokens)
                FROM proxy_request_logs {where}
                {"AND " if where else "WHERE "}NOT (app_type = 'claude'
                     OR (app_type = 'claude-desktop'
                         AND data_source = 'session_log'))
                GROUP BY app_type, input_token_semantics""", args):
            d = agg.setdefault(r[0], {"requests": 0, "input": 0,
                                      "output": 0, "cache_read": 0,
                                      "cache_creation": 0})
            d["requests"] += r[2] or 0
            d["output"] += r[4] or 0
            d["cache_read"] += r[5] or 0
            d["cache_creation"] += r[6] or 0
            d["input"] += _pure_input(r[1], r[3], r[5], r[6])
    except Exception:
        pass
    pcost = {}
    try:
        for r in cur.execute(f"""
                SELECT app_type, SUM(CAST(total_cost_usd AS REAL))
                FROM proxy_request_logs {where} GROUP BY app_type""", args):
            pcost[r[0]] = r[1] or 0.0
    except Exception:
        pass
    nmodels = {}
    try:
        for r in cur.execute(f"""
                SELECT app_type, COUNT(DISTINCT model)
                FROM proxy_request_logs {where} GROUP BY app_type""", args):
            nmodels[r[0]] = r[1] or 0
    except Exception:
        pass
    out = []
    seen = set()
    for app, a in agg.items():
        seen.add(app)
        s = sess.get(app, {})
        inp = a["input"] + s.get("input", 0)
        outp = a["output"] + s.get("output", 0)
        cr = a["cache_read"] + s.get("cache_read", 0)
        cc = a["cache_creation"] + s.get("cache_creation", 0)
        proxy_n = a["requests"]
        sess_n = s.get("requests", 0)
        denom = inp + cr + cc
        out.append({
            "name": CCS_TOOL_LABELS.get(app, app),
            "app_type": app, "available": True, "source": "ccs:proxy+session",
            "requests": proxy_n + sess_n,
            "requests_proxy": proxy_n, "requests_session": sess_n,
            "input": inp, "output": outp, "cache_read": cr,
            "cache_creation": cc,
            "total_tokens": inp + outp + cr + cc,
            "cache_hit_rate": round(cr / denom, 4) if denom else 0.0,
            "models_count": nmodels.get(app, 0),
            "cost_usd": round(pcost.get(app, 0.0) + s.get("cost", 0.0), 4),
        })
    # ---- Claude / Claude Desktop：本机 jsonl 直读（2026-10-02，绕过 CCS）----
    # token/请求数以 ~/.claude/projects 的逐条 usage 为权威（entrypoint 区分
    # 桌面端/CLI）；成本沿用 CCS 对 session_log 的估算，按 session_id 归属拆分。
    # desktop 的 CCS proxy 行（聊天走代理，不落 jsonl）保留在主循环里。
    try:
        nat_all = [r for r in _claude_native_rows(since_ts)
                   if since_ts is None or r["ts"] >= since_ts]
        desk_sessions = {r["session"] for r in nat_all
                         if r["entrypoint"] == "claude-desktop"}

        def _nat_agg(rows):
            inp = sum(r["input"] for r in rows)
            outp = sum(r["output"] for r in rows)
            cr = sum(r["cache_read"] for r in rows)
            cc = sum(r["cache_creation"] for r in rows)
            denom = inp + cr + cc
            return {
                "requests": len(rows),
                "sessions": len({r["session"] for r in rows if r["session"]}),
                "input": inp, "output": outp, "cache_read": cr,
                "cache_creation": cc, "total_tokens": inp + outp + cr + cc,
                "cache_hit_rate": round(cr / denom, 4) if denom else 0.0,
                "models_count": len({r["model"] for r in rows if r["model"]}),
            }

        # session_log 的 claude 成本按会话归属拆分（桌面端会话 → desktop）
        cost_cl, cost_desk = 0.0, 0.0
        for r in cur.execute(f"""
                SELECT session_id, SUM(CAST(total_cost_usd AS REAL))
                FROM proxy_request_logs {where}
                  {"AND" if where else "WHERE"} data_source = 'session_log'
                  AND app_type = 'claude' GROUP BY session_id""", args):
            if r[0] and r[0] in desk_sessions:
                cost_desk += r[1] or 0.0
            else:
                cost_cl += r[1] or 0.0

        n_cl = _nat_agg([r for r in nat_all
                         if r["entrypoint"] != "claude-desktop"])
        n_desk = _nat_agg([r for r in nat_all
                           if r["entrypoint"] == "claude-desktop"])

        # claude（CLI）：纯 native
        if n_cl["requests"]:
            out.append({
                "name": CCS_TOOL_LABELS.get("claude", "claude"),
                "app_type": "claude", "available": True,
                "source": "claude:native",
                "requests": n_cl["requests"], "requests_proxy": 0,
                "requests_session": 0,
                "input": n_cl["input"], "output": n_cl["output"],
                "cache_read": n_cl["cache_read"],
                "cache_creation": n_cl["cache_creation"],
                "total_tokens": n_cl["total_tokens"],
                "cache_hit_rate": n_cl["cache_hit_rate"],
                "models_count": n_cl["models_count"],
                "cost_usd": round(cost_cl, 4),
                "sessions": n_cl["sessions"],
            })
        # claude-desktop：主循环里的 proxy 行 + native（Cowork/Code 会话）
        drow = next((t for t in out if t.get("app_type") == "claude-desktop"),
                    None)
        if drow is not None:
            drow["input"] += n_desk["input"]
            drow["output"] += n_desk["output"]
            drow["cache_read"] += n_desk["cache_read"]
            drow["cache_creation"] += n_desk["cache_creation"]
            drow["total_tokens"] = (drow["input"] + drow["output"]
                                    + drow["cache_read"]
                                    + drow["cache_creation"])
            drow["requests"] += n_desk["requests"]
            denom = drow["cache_read"] + drow["input"] + drow["cache_creation"]
            drow["cache_hit_rate"] = (round(drow["cache_read"] / denom, 4)
                                      if denom else 0.0)
            drow["cost_usd"] = round(drow["cost_usd"] + cost_desk, 4)
            drow["sessions"] = n_desk["sessions"]
            drow["source"] = "claude-desktop:native+proxy"
        elif n_desk["requests"]:
            out.append({
                "name": CCS_TOOL_LABELS.get("claude-desktop", "claude-desktop"),
                "app_type": "claude-desktop", "available": True,
                "source": "claude-desktop:native",
                "requests": n_desk["requests"], "requests_proxy": 0,
                "requests_session": 0,
                "input": n_desk["input"], "output": n_desk["output"],
                "cache_read": n_desk["cache_read"],
                "cache_creation": n_desk["cache_creation"],
                "total_tokens": n_desk["total_tokens"],
                "cache_hit_rate": n_desk["cache_hit_rate"],
                "models_count": n_desk["models_count"],
                "cost_usd": round(cost_desk, 4),
                "sessions": n_desk["sessions"],
            })
    except Exception:
        pass
    # 只有会话同步数据的 app_type（无 proxy 记录）
    for app, s in sess.items():
        if app in seen:
            continue
        denom = s["input"] + s["cache_read"] + s["cache_creation"]
        out.append({
            "name": CCS_TOOL_LABELS.get(app, app),
            "app_type": app, "available": True, "source": "ccs:session",
            "requests": s["requests"], "requests_proxy": 0,
            "requests_session": s["requests"],
            "input": s["input"], "output": s["output"],
            "cache_read": s["cache_read"], "cache_creation": s["cache_creation"],
            "total_tokens": (s["input"] + s["output"] + s["cache_read"]
                             + s["cache_creation"]),
            "cache_hit_rate": round(s["cache_read"] / denom, 4) if denom else 0.0,
            "cost_usd": round(s["cost"], 4),
        })
    out.sort(key=lambda t: -t["total_tokens"])
    con.close()
    return out


# ---------------------------------------------------------------- Codex
# Codex 本地 rollout 会话文件（权威 token 记账，覆盖全部线路）：
#   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl + ~/.codex/archived_sessions/
# 每文件最后一条 token_count 事件的 total_token_usage 即全会话累计
# （实测与 state_5.sqlite threads.tokens_used 逐字节一致）。
# 线路事实（2026-09 实测）：Codex 走 CCS(15721) 与 Codex++(57321) 两条代理，
# Codex++ 为纯转发，CCS 代理日志只记它接管期间的流量——故 token 以 rollout
# 为准。成本取 CCS 侧两种记账之和：代理期真实计价（data_source='proxy'）
# + 会话同步估算（data_source='codex_session'，cc-switch 从本地会话文件同步、
# 按模型单价折算，覆盖 CCS 运行期间走 Codex++ 的流量）；仅 CCS 离线时段缺计价。
CODEX_SESSIONS_DIR = os.path.expanduser(r"~\.codex\sessions")
CODEX_ARCHIVED_DIR = os.path.expanduser(r"~\.codex\archived_sessions")
_CODEX_MEM = {"files": {}, "paths": [], "listed_at": 0.0}
_CODEX_LOCK = threading.Lock()


def _codex_rollout_files():
    out = glob.glob(os.path.join(
        CODEX_SESSIONS_DIR, "*", "*", "*", "rollout-*.jsonl"))
    out += glob.glob(os.path.join(CODEX_ARCHIVED_DIR, "rollout-*.jsonl"))
    return out


def _codex_paths():
    """rollout 文件列表（5 秒缓存：目录遍历不必每个请求都做一遍）。"""
    now = time.time()
    if _CODEX_MEM["paths"] and now - _CODEX_MEM["listed_at"] < 5:
        return _CODEX_MEM["paths"]
    paths = _codex_rollout_files()
    _CODEX_MEM["paths"] = paths
    _CODEX_MEM["listed_at"] = now
    return paths


def _codex_parse_file(path, mtime):
    """解析单个 rollout 文件，返回 (per-file 行, 模型明细行, 逐事件行)。

    per-file: (mtime, requests, input, cached, cache_write, output)，
    取文件末尾 total_token_usage（全会话累计）。
    模型明细：按逐响应 last_token_usage 的占比，把该文件的累计值分摊到
    文件里出现过的各模型上——会话续跑/压缩会让「逐响应求和」比累计值少
    20%+，直接用心智模型明细之和会小于卡片总量（环形图看起来「统计不全」）。
    逐事件行 (ts, 纯input, output, cache_read, cache_write) 供 pulse 的
    分钟/小时桶使用——与累计值同一次读盘产出，避免同一批文件被解析两遍。
    """
    last = None
    reqs = 0
    current_model = None
    previous_snapshot = None
    model_usage = {}
    events = []
    try:
        # 二进制读 + bytes 子串预筛（rollout 全量 1.2GB，比文本模式快约 40%）；
        # 命中的行才 json.loads，损坏的 UTF-8 由 json 异常兜住不影响全文件。
        with open(path, "rb") as fh:
            for line in fh:
                if b'"turn_context"' in line:
                    try:
                        entry = json.loads(line)
                        payload = entry.get("payload") or {}
                        if entry.get("type") == "turn_context":
                            current_model = payload.get("model") or current_model
                    except Exception:
                        pass
                if b'"last_token_usage"' in line:
                    reqs += 1
                    last = line
                    try:
                        entry = json.loads(line)
                        info = ((entry.get("payload") or {}).get("info") or {})
                        usage = info.get("last_token_usage") or {}
                        total_usage = info.get("total_token_usage") or {}
                        snapshot = tuple(total_usage.get(k, 0) or 0 for k in (
                            "input_tokens", "cached_input_tokens",
                            "cache_write_input_tokens", "output_tokens"))
                        # rollout 有少量重复发出的 token_count 快照；同一累计值只计一次。
                        if snapshot != previous_snapshot:
                            model_name = current_model or "未识别模型"
                            row = model_usage.setdefault(model_name, [0, 0, 0, 0, 0])
                            row[0] += 1
                            row[1] += usage.get("input_tokens", 0) or 0
                            row[2] += usage.get("cached_input_tokens", 0) or 0
                            row[3] += usage.get("cache_write_input_tokens", 0) or 0
                            row[4] += usage.get("output_tokens", 0) or 0
                            ts = _timestamp_epoch(entry.get("timestamp"))
                            if ts:
                                e_inp = usage.get("input_tokens", 0) or 0
                                e_cached = usage.get("cached_input_tokens", 0) or 0
                                e_cw = usage.get("cache_write_input_tokens", 0) or 0
                                e_out = usage.get("output_tokens", 0) or 0
                                if e_cached > e_inp:
                                    e_pure, e_cr = e_inp, e_cached
                                else:
                                    e_pure, e_cr = e_inp - e_cached, e_cached
                                events.append((ts, e_pure, e_out, e_cr, e_cw, model_name))
                        previous_snapshot = snapshot
                    except Exception:
                        # 不让一个损坏的事件影响全文件累计值。
                        pass
                elif last is None and b"total_token_usage" in line:
                    last = line
    except OSError:
        return None, [], []
    if not last:
        return None, [], events
    try:
        info = ((json.loads(last).get("payload") or {}).get("info") or {})
        u = info.get("total_token_usage") or {}
    except Exception:
        return None, [], events
    if not u:
        return None, [], events
    row = (mtime, reqs,
           u.get("input_tokens", 0) or 0,
           u.get("cached_input_tokens", 0) or 0,
           u.get("cache_write_input_tokens", 0) or 0,
           u.get("output_tokens", 0) or 0)
    cum_in, cum_cached, cum_cw, cum_out = row[2], row[3], row[4], row[5]
    weights = {name: (v[1] + (v[2] if v[2] > v[1] else 0) + v[4])
               for name, v in model_usage.items()}
    total_w = sum(weights.values())
    model_rows = []
    if total_w > 0:
        for name, v in model_usage.items():
            share = weights[name] / total_w
            model_rows.append((mtime, name, v[0],
                               int(round(cum_in * share)),
                               int(round(cum_cached * share)),
                               int(round(cum_cw * share)),
                               int(round(cum_out * share))))
    elif cum_in or cum_out:
        model_rows.append((mtime, "未识别模型", reqs,
                           cum_in, cum_cached, cum_cw, cum_out))
    return row, model_rows, events


def _codex_scan(since_ts=None):
    """按窗口刷新 rollout per-file 解析缓存，返回窗口内文件 {path: 缓存项}。

    缓存项: (key, per-file 行, 模型明细行, 逐事件行)，key = (size, mtime)。
    只解析「窗口内且 (size, mtime) 变化」的文件：默认 7 天窗口只需读
    30/358 个文件（约 0.4GB），全量 1.2GB 只在选「全部」或后台预热时读，
    因此冷启动不再为一次汇总扫完所有历史（2026-10-08）。
    解析在锁外进行、写入加锁，后台预热与请求线程不会长时间互相阻塞。
    """
    paths = _codex_paths()
    cache = _CODEX_MEM["files"]
    live = set()
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        live.add(p)
        # 宽放 2 天初筛：Windows 上 Codex 长会话可能保持初始 mtime（如昨夜 23:55 创建、跑入今日凌晨），
        # 若严格按 since_ts 过滤会把跨夜/跨自然日的今日用量整盘漏掉；留给 _codex_window_ratio 精确切分。
        if since_ts and st.st_mtime < (since_ts - 172800):
            continue
        key = (st.st_size, int(st.st_mtime))
        with _CODEX_LOCK:
            hit = cache.get(p)
            if hit and hit[0] == key:
                continue
        parsed = _codex_parse_file(p, st.st_mtime)
        with _CODEX_LOCK:
            cache[p] = (key,) + parsed
    with _CODEX_LOCK:
        for p in [k for k in cache if k not in live]:
            cache.pop(p, None)   # 已删除的文件不留缓存
        return {
            p: item for p, item in cache.items()
            if not since_ts or (
                any(ev[0] >= since_ts for ev in item[3])
                if item[3] else item[0][1] >= since_ts
            )
        }


def _codex_window_ratio(events, since_ts):
    """文件累计的时间窗占比：窗口内事件 token 权重 / 全文件事件权重。

    events 为 _codex_parse_file 产出的逐响应行
    (ts, 纯input, output, cache_read, cache_write, model)。
    「全部」窗口恒 1.0；文件没有带时间戳的事件时退回 1.0
    （累计整窗计入，与旧 mtime 落窗口径一致）。
    """
    if since_ts is None:
        return 1.0
    all_w = win_w = 0
    for ev in events:
        w = ev[1] + ev[2] + ev[3] + ev[4]
        all_w += w
        if ev[0] >= since_ts:
            win_w += w
    return win_w / all_w if all_w > 0 else 1.0


def _codex_models(since_ts=None, per=None):
    """按 Codex rollout 归并模型用量（时间窗口径）。

    模型窗口量 = 文件累计 ×（该模型窗口内事件 token 权重 / 文件全部事件
    权重）——与 codex_stats 卡片同用一套 events 权重、同一分母 f_all，
    取整前各模型 total 之和严格等于卡片总量（Σ m_win_m/f_all == file_ratio）。
    会话续跑/压缩只按 last 记账会比文件累计少 20%+，因此事件只定分布、
    绝对量仍以文件累计为基。无带时间戳事件的文件沿用预制模型行整窗计入
    （此时权重 1.0，与旧 mtime 口径一致）。requests 按窗口内去重响应事件
    计数。per 传入同一次扫描结果可省一遍 stat。"""
    grouped = {}
    for _path, item in (per if per is not None else _codex_scan(since_ts)).items():
        m_all, m_win, m_cnt = {}, {}, {}
        f_all = 0
        for ev in item[3]:
            w = ev[1] + ev[2] + ev[3] + ev[4]
            f_all += w
            name = ev[5]
            m_all[name] = m_all.get(name, 0) + w
            if since_ts is None or ev[0] >= since_ts:
                m_win[name] = m_win.get(name, 0) + w
                m_cnt[name] = m_cnt.get(name, 0) + 1
        if not item[1]:
            continue
        _mtime, n, cum_in, cum_cached, cum_cw, cum_out = item[1]
        if f_all > 0:
            for name in m_all:
                w_win = m_win.get(name, 0)      # 「全部」窗口时 == m_all[name]
                row = grouped.setdefault(name, {
                    "model": name, "requests": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cache_write": 0,
                })
                row["requests"] += m_cnt.get(name, 0)
                row["input"] += int(round(cum_in * w_win / f_all))
                row["output"] += int(round(cum_out * w_win / f_all))
                row["cache_read"] += int(round(cum_cached * w_win / f_all))
                row["cache_write"] += int(round(cum_cw * w_win / f_all))
        else:
            # 无带时间戳事件的文件：沿用预制分摊行，整窗计入（与旧口径一致）
            for mtime, model, requests, inp, cached, cache_write, outp in item[2]:
                row = grouped.setdefault(model, {
                    "model": model, "requests": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cache_write": 0,
                })
                row["requests"] += requests
                row["input"] += inp
                row["output"] += outp
                row["cache_read"] += cached
                row["cache_write"] += cache_write
    result = []
    for row in grouped.values():
        # Codex/OpenAI rollout input normally includes cache reads. One observed
        # custom provider reports cache_read separately (cache > input); account
        # for that case instead of showing an impossible >100% hit rate.
        cache_is_separate = row["cache_read"] > row["input"]
        input_side = row["input"] + (row["cache_read"] if cache_is_separate else 0)
        row["total_tokens"] = input_side + row["output"]
        row["cache_hit_rate"] = round(row["cache_read"] / input_side, 4) if input_side else 0.0
        result.append(row)
    return sorted(result, key=lambda row: row["total_tokens"], reverse=True)


def _codex_ccs_cost(since_ts=None):
    """CCS 侧记到的 Codex 成本（rollout 文件本身无计价）。

    含两种 data_source：'proxy'（代理期真实计价）+ 'codex_session'
    （会话同步、按模型单价估算，覆盖 CCS 运行期间走 Codex++ 的流量，
    两种来源 session 不重叠）；CCS 离线时段仍缺。
    """
    if not os.path.exists(CCS_DB):
        return 0.0, 0
    con = _ro(CCS_DB)
    cur = con.cursor()
    sql = ("SELECT COUNT(*), SUM(CAST(total_cost_usd AS REAL)) "
           "FROM proxy_request_logs WHERE app_type='codex'")
    args = []
    if since_ts:
        sql += " AND created_at >= ?"
        args.append(int(since_ts))
    r = _sum_row(cur, sql, args)
    con.close()
    return (r[1] or 0.0) if r else 0.0, (r[0] if r else 0)


def codex_stats(since_ts=None):
    """Codex（全线路：CCS 代理 / Codex++ / 官方直连）token 用量。

    口径（OpenAI）：input_tokens **已包含** cached_input_tokens
    （cache_write_input_tokens 亦在内）→
      纯 input = input - cached - cache_write；统一 total = input + output；
      命中率 = cached / input（同 ZCode 的 OpenAI 风格）。
    时间窗（2026-10-11）：先按文件 mtime 选出可能含窗口事件的文件
    （事件时间戳 ≤ mtime，窗口外文件不可能有窗口内事件），再把文件累计值
    按逐响应事件 token 权重切分进窗口——长寿会话只有窗口内增量计入当前
    窗口；「全部」窗口权重恒 1.0，与历史累计口径严格一致。
    requests 为窗口内去重响应事件数（同一 total 快照的重复行只计一次）。
    """
    per = _codex_scan(since_ts)
    files = reqs = 0
    inp = cached = cw = outp = 0.0
    for _path, item in per.items():
        if not item[1]:         # 没有累计值的文件（无 token_count 事件）不计入
            continue
        _mtime, n, i, c, w, o = item[1]
        events = item[3]
        ratio = _codex_window_ratio(events, since_ts)
        if events:
            win_reqs = sum(1 for ev in events
                           if since_ts is None or ev[0] >= since_ts)
        else:
            win_reqs = n        # 无事件文件整窗计入（权重 1）
        files += 1
        reqs += win_reqs
        inp += i * ratio
        cached += c * ratio
        cw += w * ratio
        outp += o * ratio
    if not files:
        return {"name": "Codex", "group": "Codex", "available": False,
                "reason": "未找到 Codex 会话 rollout 文件"}
    cost, ccs_reqs = _codex_ccs_cost(since_ts)
    inp = int(round(inp))
    cached = int(round(cached))
    cw = int(round(cw))
    outp = int(round(outp))
    pure_in = max(inp - cached - cw, 0)
    return {
        "name": "Codex", "group": "Codex", "available": True,
        "source": "codex:rollout",
        "sessions": files, "requests": reqs,
        "input": pure_in, "output": outp,
        "cache_read": cached, "cache_write": cw,
        "total_tokens": inp + outp,
        "cache_hit_rate": round(cached / inp, 4) if inp else 0.0,
        "models": _codex_models(since_ts, per),
        "cost_usd": round(cost, 4) if cost else None,
        "note": ("token 为 rollout 全线路记账；成本为 CCS 侧记账 "
                 f"（{ccs_reqs} 次：代理期真实计价 + 会话同步按模型单价估算），"
                 "CCS 未运行的时段无计价"),
    }


def _ls_info():
    """发现 Antigravity 语言服务器的 (port, csrf_token)。

    端口/令牌每次启动都变；成功结果缓存 60 秒，失败只缓存 5 秒。
    优先采用 PowerShell Get-CimInstance，快速且不依赖弃用的 wmic；
    并对监听端口进行 Connect 协议验证，避免误取普通 HTTP 端口。
    """
    now = time.time()
    ttl = 60 if _LS_CACHE["port"] else 5
    if _LS_CACHE["ts"] and now - _LS_CACHE["ts"] < ttl:
        return _LS_CACHE["port"], _LS_CACHE["token"]
    pid = port = token = None

    # 1. 优先 PowerShell CIM 获取（秒级，不卡 WMI）
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \\\"Name = 'language_server.exe'\\\" | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        # creationflags=CREATE_NO_WINDOW：windowed（无控制台）运行时防止闪现黑窗
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        if out:
            data = json.loads(out)
            if isinstance(data, list):
                data = data[0] if data else {}
            pid = str(data.get("ProcessId") or "")
            cmd = data.get("CommandLine") or ""
            m2 = re.search(r"--csrf_token[= ]+([0-9a-fA-F-]{8,})", cmd)
            if m2:
                token = m2.group(1)
    except Exception:
        pid = token = None

    # 2. 回退 wmic
    if not pid:
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "Name='language_server.exe'",
                 "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW).stdout
            for line in out.splitlines():
                if "language_server.exe" not in line:
                    continue
                m = re.search(r"(\d+)\s*$", line.strip())
                if m:
                    pid = m.group(1)
                m2 = re.search(r"--csrf_token[= ]+([0-9a-fA-F-]{8,})", line)
                if m2:
                    token = m2.group(1)
                if pid:
                    break
        except Exception:
            pass

    # 3. 取监听端口，并用 Connect 协议校验（避免误取普通 HTTP端口）
    if pid:
        try:
            nets = subprocess.run(["netstat", "-ano"], capture_output=True,
                                  text=True, timeout=15,
                                  errors="replace",
                                  creationflags=subprocess.CREATE_NO_WINDOW).stdout
            ports = sorted({int(re.search(r"127\.0\.0\.1:(\d+)", l).group(1))
                            for l in nets.splitlines()
                            if "LISTENING" in l and f" {pid}" in l
                            and re.search(r"127\.0\.0\.1:(\d+)", l)})
            if ports:
                import ssl
                import urllib.request
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE        # 本地 LS 自签证书
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPSHandler(context=ctx))
                for p in ports:
                    try:
                        url = f"https://127.0.0.1:{p}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"

                        body = json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode()
                        req = urllib.request.Request(url, data=body,
                                                     headers={"Content-Type": "application/json",
                                                              "Connect-Protocol-Version": "1",
                                                              "X-Codeium-Csrf-Token": token or ""})

                        with op.open(req, timeout=1.5) as r:
                            if r.status == 200:
                                port = p
                                break
                    except Exception:
                        continue
                if not port:
                    port = ports[0]
        except Exception:
            port = None
    _LS_CACHE.update({"ts": now, "port": port, "token": token})
    return port, token


def _quota_rpc(port, token, timeout=10):
    """POST 本地 LS 的 RetrieveUserQuotaSummary（Connect 协议）。"""
    import ssl
    import urllib.request
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
    ctx.verify_mode = ssl.CERT_NONE        # 本地 LS 自签证书
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPSHandler(context=ctx))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _base_dir():
    """缓存/产物目录：打包后在 exe 同目录，开发时在模块目录。"""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


_AG_CACHE_FILE = os.path.join(_base_dir(), "antigravity_agg.json")
_AG_MEM = {"loaded": False, "data": {}}


def _ls_call(method, body, timeout=60):
    """调本地语言服务器的 Connect RPC。"""
    import ssl
    import urllib.request
    port, token = _ls_info()
    if not port:
        raise RuntimeError("Antigravity 未运行")
    url = (f"https://127.0.0.1:{port}/exa.language_server_pb."
           f"LanguageServerService/{method}")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Connect-Protocol-Version": "1",
                 "X-Codeium-Csrf-Token": token or ""})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPSHandler(context=ctx))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _ag_load_cache():
    if _AG_MEM["loaded"]:
        return _AG_MEM["data"]
    try:
        with open(_AG_CACHE_FILE, encoding="utf-8") as f:
            _AG_MEM["data"] = json.load(f)
    except Exception:
        _AG_MEM["data"] = {}
    _AG_MEM["loaded"] = True
    return _AG_MEM["data"]

def _ag_save_cache(data):
    _AG_MEM["data"] = data
    try:
        with open(_AG_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


_AG_BRAIN_DIR = os.path.expanduser(r"~\.gemini\antigravity\brain")
_AG_STATE_FILE = os.path.expanduser(r"~\.gemini\antigravity\antigravity_state.pbtxt")
_AG_NATIVE_MEM = {"files": {}}
_AG_NATIVE_LOCK = threading.Lock()

_ANTIGRAVITY_MODEL_MAP = {
    "MODEL_PLACEHOLDER_M318": "Gemini 3.8 Flash (High)",
    "MODEL_PLACEHOLDER_M319": "Gemini 3.8 Flash (Medium)",
    "MODEL_PLACEHOLDER_M320": "Gemini 3.8 Flash (Low)",
    "MODEL_PLACEHOLDER_M322": "gemini-3.8-flash-tiered",
    "MODEL_PLACEHOLDER_M298": "Gemini 3.7 Flash (High)",
    "MODEL_PLACEHOLDER_M299": "Gemini 3.7 Flash (Medium)",
    "MODEL_PLACEHOLDER_M300": "Gemini 3.7 Flash (Low)",
    "MODEL_PLACEHOLDER_M301": "gemini-3.7-flash-tiered",
    "MODEL_PLACEHOLDER_M71": "Gemini 3.6 Flash (High)",
    "MODEL_PLACEHOLDER_M72": "Gemini 3.6 Flash (Medium)",
    "MODEL_PLACEHOLDER_M73": "Gemini 3.6 Flash (Low)",
    "MODEL_PLACEHOLDER_M196": "gemini-3.6-flash-tiered",
    "MODEL_PLACEHOLDER_M84": "Gemini 3.5 Flash (High)",
    "MODEL_PLACEHOLDER_M20": "Gemini 3.5 Flash (Medium)",
    "MODEL_PLACEHOLDER_M187": "Gemini 3.5 Flash (Low)",
    "MODEL_PLACEHOLDER_M198": "Gemini 3.5 Flash Lite",
    "MODEL_PLACEHOLDER_M18": "Gemini 3 Flash",
    "MODEL_PLACEHOLDER_M37": "Gemini 3.1 Pro (High)",
    "MODEL_PLACEHOLDER_M36": "Gemini 3.1 Pro (Low)",
    "MODEL_PLACEHOLDER_M16": "Gemini 3.1 Pro (High)",
    "MODEL_PLACEHOLDER_M50": "Gemini 3.1 Flash Lite",
    "MODEL_PLACEHOLDER_M21": "Gemini 3.1 Flash Image",
    "MODEL_PLACEHOLDER_M26": "Claude Opus 4.6 (Thinking)",
    "MODEL_PLACEHOLDER_M35": "Claude Sonnet 4.6 (Thinking)",
    "MODEL_GOOGLE_GEMINI_2_5_PRO": "Gemini 2.5 Pro",
    "MODEL_GOOGLE_GEMINI_2_5_FLASH": "Gemini 3.5 Flash Lite",
    "MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE": "Gemini 3.5 Flash Lite",
    "MODEL_OPENAI_GPT_OSS_120B_MEDIUM": "GPT-OSS 120B (Medium)",
}


def _resolve_ag_model(model_id):
    """将 Antigravity 内部占位符转换为友好模型名称。"""
    if not model_id:
        return "Gemini (Antigravity)"
    if model_id in _ANTIGRAVITY_MODEL_MAP:
        return _ANTIGRAVITY_MODEL_MAP[model_id]
    if not model_id.startswith("MODEL_"):
        return model_id
    parts = model_id.split("_")
    return f"Gemini ({parts[-1]})"


def _get_ag_default_model():
    """从 antigravity_state.pbtxt 读取用户在 IDE 中当前选择的模型。"""
    try:
        if os.path.isfile(_AG_STATE_FILE):
            with open(_AG_STATE_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "last_selected_agent_model:" in line:
                        m = line.split(":", 1)[1].strip()
                        if m:
                            return m
    except Exception:
        pass
    return ""


def _antigravity_parse_transcript(log_path, cid):
    """解析单个 Antigravity 会话的 transcript.jsonl 为逐事件行。"""
    rows = []
    model_raw = ""
    try:
        cache = _ag_load_cache()
        c_info = cache.get(cid)
        if c_info and c_info.get("models"):
            def _m_weight(m):
                v = c_info["models"].get(m)
                return v[0] if isinstance(v, (list, tuple)) else (v or 0)
            model_raw = max(c_info["models"], key=_m_weight)
    except Exception:
        pass
    if not model_raw:
        model_raw = _get_ag_default_model()
    model_name = _resolve_ag_model(model_raw)

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "PLANNER_RESPONSE" not in line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") != "PLANNER_RESPONSE":
                        continue
                    cat = d.get("created_at")
                    if not cat:
                        continue
                    ts = int(datetime.datetime.fromisoformat(cat.replace("Z", "+00:00")).timestamp())
                    rows.append({
                        "ts": ts,
                        "app": "antigravity",
                        "model": model_name,
                        "input": int(d.get("input_tokens") or 0),
                        "output": int(d.get("output_tokens") or 0),
                        "cache_read": int(d.get("cache_read_tokens") or 0),
                        "cache_creation": 0,
                        "cost": 0.0,
                        "status": 200 if d.get("status") == "DONE" else (500 if d.get("status") == "ERROR" else 200),
                        "latency_ms": 0,
                        "session": cid,
                    })
                except Exception:
                    continue
    except OSError:
        return []
    return rows


def _antigravity_native_rows(since_ts=None):
    """Antigravity 本机逐条调用（读 brain/*/transcript.jsonl，带 per-file mtime 缓存）。"""
    if not os.path.isdir(_AG_BRAIN_DIR):
        return []
    cache = _AG_NATIVE_MEM["files"]
    live = set()
    try:
        entries = list(os.scandir(_AG_BRAIN_DIR))
    except OSError:
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        log_path = os.path.join(entry.path, ".system_generated", "logs", "transcript.jsonl")
        try:
            st = os.stat(log_path)
        except OSError:
            continue
        live.add(log_path)
        if since_ts and st.st_mtime < since_ts:
            continue
        key = (st.st_size, int(st.st_mtime))
        with _AG_NATIVE_LOCK:
            hit = cache.get(log_path)
            if hit and hit[0] == key:
                continue
        rows = _antigravity_parse_transcript(log_path, entry.name)
        with _AG_NATIVE_LOCK:
            cache[log_path] = (key, rows)

    with _AG_NATIVE_LOCK:
        for p in [k for k in cache if k not in live]:
            cache.pop(p, None)
        out = []
        for item in cache.values():
            for r in item[1]:
                if since_ts and r["ts"] < since_ts:
                    continue
                out.append(r)
    return out


def antigravity_tokens(days=None, max_conversations=30, since=None):
    """Antigravity 真实 token 用量（本地 LS RPC + 本地 DB 全量历史）。

    权威会话源：`~/.gemini/antigravity/conversation_summaries.db`（记录全量会话）
    记账源：本地 LS `GetCascadeTrajectorySteps` 返回每步真实 modelUsage。
    缓存：按 (conversation_id, last_modified_time) 持久化在 `antigravity_agg.json`。
    优雅降级：LS 离线时无缝读取全量持久化历史缓存，不灰显或丢失指标。
    """
    cache = _ag_load_cache()
    changed = False

    # LS 在线状态（离线只能读缓存）
    ls_alive = False
    try:
        port, token = _ls_info()
        if port:
            ls_alive = True
    except Exception:
        pass

    # 会话清单：本地 DB 优先，LS 兜底
    conv_items = []
    if os.path.exists(ANTIGRAVITY_DB):
        try:
            con = _ro(ANTIGRAVITY_DB)
            rows = con.execute(
                "SELECT conversation_id, last_modified_time, title "
                "FROM conversation_summaries ORDER BY last_modified_time DESC"
            ).fetchall()
            con.close()
            conv_items = [(r[0], r[1] or "", r[2] or "") for r in rows]
        except Exception:
            conv_items = []

    if not conv_items and ls_alive:
        try:
            trajs = _ls_call("GetAllCascadeTrajectories", {})
            sums = trajs.get("trajectorySummaries", {}) or {}
            conv_items = [(cid, s.get("lastModifiedTime") or "", s.get("summary") or "")
                          for cid, s in sums.items()]
        except Exception:
            pass

    # 逐会话取步级记账（带抓取预算）
    if ls_alive and conv_items:
        fetch_budget = 10
        for cid, mtime, title in conv_items[:max_conversations]:
            c = cache.get(cid)
            if c and c.get("mtime") == mtime:
                continue
            if fetch_budget <= 0:
                break
            try:
                steps = _ls_call("GetCascadeTrajectorySteps", {"cascadeId": cid},
                                 timeout=15).get("steps", [])
            except Exception:
                continue
            calls = 0
            inp = out = cache_r = 0
            byday = {}
            models = {}
            for st in steps:
                md = st.get("metadata") or {}
                mu = md.get("modelUsage")
                if not mu:
                    continue
                calls += 1
                i = int(mu.get("inputTokens") or 0)
                o = int(mu.get("outputTokens") or 0)
                cr = int(mu.get("cacheReadTokens") or 0)
                inp += i
                out += o
                cache_r += cr
                day = (md.get("createdAt") or "")[:10]
                if day:
                    b = byday.setdefault(day, [0, 0, 0, 0])
                    b[0] += 1
                    b[1] += i
                    b[2] += o
                    b[3] += cr
                gm = md.get("generatorModel")
                name = gm if isinstance(gm, str) else mu.get("model") or "(unknown)"
                m_entry = models.setdefault(name, [0, 0, 0, 0])
                m_entry[0] += 1
                m_entry[1] += i
                m_entry[2] += o
                m_entry[3] += cr

            cache[cid] = {
                "mtime": mtime, "summary": (title or ""),
                "steps": len(steps), "calls": calls,
                "input": inp, "output": out, "cache_read": cache_r,
                "byday": byday, "models": models,
            }
            changed = True
            fetch_budget -= 1

    if changed:
        _ag_save_cache(cache)

    if not cache:
        return {"name": "Antigravity", "available": False,
                "reason": "未找到任何 Antigravity 会话数据（LS 离线且无本地缓存）"}

    # 窗口过滤：按日聚合的裁剪（支持 since 秒级时间戳精确对齐本地时区自然日）
    since_date = None
    if since is not None:
        try:
            since_date = datetime.datetime.fromtimestamp(float(since)).strftime("%Y-%m-%d")
        except Exception:
            pass
    elif days:
        try:
            since_date = (datetime.datetime.now() - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d")
        except Exception:
            pass

    tot = {"sessions": 0, "requests": 0, "input": 0, "output": 0, "cache_read": 0}
    byday_all = {}
    models_all = {}
    # 逐会话汇总
    for cid, c in cache.items():
        byday = c.get("byday") or {}
        tot_calls = c.get("calls") or 1
        if since_date:
            active_in_window = False
            session_req = session_inp = session_out = session_cr = 0
            for d, v in byday.items():
                if d >= since_date:
                    active_in_window = True
                    session_req += v[0]
                    session_inp += v[1]
                    session_out += v[2]
                    session_cr += v[3]
                    b = byday_all.setdefault(d, [0, 0, 0, 0])
                    for k in range(4):
                        b[k] += v[k]
            if not active_in_window or session_req == 0:
                continue
            ratio = session_req / tot_calls if tot_calls else 0.0
            tot["sessions"] += 1
            tot["requests"] += session_req
            tot["input"] += session_inp
            tot["output"] += session_out
            tot["cache_read"] += session_cr
        else:
            ratio = 1.0
            tot["sessions"] += 1
            tot["requests"] += c.get("calls", 0)
            tot["input"] += c.get("input", 0)
            tot["output"] += c.get("output", 0)
            tot["cache_read"] += c.get("cache_read", 0)
            for d, v in byday.items():
                b = byday_all.setdefault(d, [0, 0, 0, 0])
                for k in range(4):
                    b[k] += v[k]

        # 模型维度：兼容历史 int 计数与新 [calls, inp, outp, cr]，并按时间窗比例核算
        for m, val in (c.get("models") or {}).items():
            mapped_m = _resolve_ag_model(m)
            if isinstance(val, (list, tuple)) and len(val) >= 4:
                m_calls, m_inp, m_out, m_cr = val[0], val[1], val[2], val[3]
            elif isinstance(val, (int, float)):
                m_ratio = (val / tot_calls) if tot_calls else 0.0
                m_calls = int(val)
                m_inp = int(c.get("input", 0) * m_ratio)
                m_out = int(c.get("output", 0) * m_ratio)
                m_cr = int(c.get("cache_read", 0) * m_ratio)
            else:
                continue

            entry = models_all.setdefault(mapped_m, [0, 0, 0, 0])
            entry[0] += int(m_calls * ratio)
            entry[1] += int(m_inp * ratio)
            entry[2] += int(m_out * ratio)
            entry[3] += int(m_cr * ratio)

    denom = tot["input"] + tot["cache_read"]
    # 2026-10-14 口径修正：总量含缓存读取，与其余来源（total=纯input+output+cache）
    # 统一；旧口径 input+output 会让 Antigravity 在全站对比中被低估约 13 倍。
    tot["total_tokens"] = tot["input"] + tot["output"] + tot["cache_read"]
    tot["cache_hit_rate"] = round(tot["cache_read"] / denom, 4) if denom else 0.0
    tot["by_day"] = [{"date": d, "requests": v[0], "input": v[1],
                      "output": v[2], "cache_read": v[3]}
                     for d, v in sorted(byday_all.items())]

    tot_models = []
    for m, stats in models_all.items():
        reqs = stats[0]
        inp = stats[1]
        outp = stats[2]
        cr = stats[3]
        # 2026-10-14 口径修正：模型行总量含缓存读取（与来源级、其余来源一致）。
        tot_all = inp + outp + cr
        tot_tok = tot_all
        m_denom = inp + cr
        hit_rate = round(cr / m_denom, 4) if m_denom else 0.0
        tot_models.append({
            "model": m,
            "requests": reqs,
            "input": inp,
            "output": outp,
            "cache_read": cr,
            "total_tokens": tot_tok,
            "total": tot_all,
            "cache_hit_rate": hit_rate,
        })
    tot_models.sort(key=lambda x: x["total_tokens"], reverse=True)
    tot["models"] = tot_models

    return {
        "name": "Antigravity",
        "available": True,
        "live": ls_alive,
        "source": "antigravity:ls_rpc(modelUsage)" if ls_alive else "antigravity:cache",
        "note": "本地 LS + DB 全量真实记账" if ls_alive else "展示本地历史持久化记账（LS 当前离线）",
        "tracked_conversations": len(cache),
        "db_conversations": len(conv_items) if conv_items else len(cache),
        **tot,
    }


def antigravity_status():
    """Antigravity：官方配额 + 活动量（token 用量见 antigravity_tokens）。

    实测结论：本地文件不落地 token 用量（全树无 usageMetadata/promptTokenCount），
    但 **LS RPC 暴露真实记账**（GetCascadeTrajectorySteps 的 modelUsage）——
    见 antigravity_tokens()。官方配额接口亦可用（本地 LS RPC）。配额方法来自
    https://github.com/NaHSIT/Antigravity-Usage —— 曾自搭 mitmproxy 抓包，属弯路，已弃。
    """
    base = {"name": "Antigravity", "group": "Antigravity"}
    # 活动量（会话/步数）
    if os.path.exists(ANTIGRAVITY_DB):
        try:
            con = _ro(ANTIGRAVITY_DB)
            row = con.execute("SELECT COUNT(*), SUM(step_count) "
                              "FROM conversation_summaries").fetchone()
            base["sessions"], base["steps"] = row[0] or 0, row[1] or 0
            con.close()
        except Exception:
            pass
    port, token = _ls_info()
    if not port:
        base.update({"available": False,
                     "reason": "Antigravity 未运行（找不到语言服务器）"})
        return base
    try:
        data = _quota_rpc(port, token)
    except Exception as e:
        base.update({"available": False,
                     "reason": f"配额接口不可用: {str(e)[:60]}"})
        return base
    resp = data.get("response") or {}
    groups = []
    for g in resp.get("groups", []):
        buckets = [{
            "id": b.get("bucketId"),
            "window": b.get("window"),
            "remaining_pct": round((b.get("remainingFraction") or 0) * 100, 1),
            "reset_time": b.get("resetTime"),
            "desc": (b.get("description") or "")[:120],
        } for b in g.get("buckets", [])]
        groups.append({"name": g.get("displayName") or "?",
                       "models": (g.get("description") or "")[:160],
                       "buckets": buckets})
    base.update({
        "available": True, "kind": "quota",
        "source": "antigravity:quota_rpc",
        "note": "官方配额百分比（配套 tokens 字段：LS RPC 真实 token 记账）",
        "groups": groups,
        "quota_description": (resp.get("description") or "")[:220],
    })
    return base


def tools_overview(since=None):
    """工具/Agent 维度总览：Hermes 各档案 + CCS 各客户端 + ZCode + Antigravity。

    since：窗口起点（秒级时间戳，可选）。"""
    tools = []
    for a in hermes_agents(since):
        if "error" in a:
            continue
        tools.append({
            "name": f"Hermes·{a['name']}", "group": "Hermes",
            "available": True, "source": "hermes:sessions",
            "requests": a.get("api_calls") or a.get("sessions", 0),
            "sessions": a.get("sessions", 0),
            "input": a.get("input", 0), "output": a.get("output", 0),
            "cache_read": a.get("cache_read", 0),
            "cache_write": a.get("cache_write", 0),
            "total_tokens": (a.get("input", 0) + a.get("output", 0)
                             + a.get("cache_read", 0)
                             + a.get("cache_write", 0)),
            "cache_hit_rate": a.get("cache_hit_rate", 0.0),
            "cost_usd": a.get("cost_usd"),
            "cost_anomaly": a.get("cost_anomaly", False),
        })
    # Codex 用 rollout 全线路记账单独成行；CCS 的 codex 行剔除防双算
    # （rollout 已覆盖 CCS 代理期间的流量，成本口径见 codex_stats.note）
    for t in ccs_by_tool(since):
        if t.get("app_type") in ("codex", "pi"):
            continue
        t["group"] = "CCS"
        tools.append(t)
    cx = codex_stats(since)
    tools.append(cx)
    ds = dsh_stats(since)
    ds["group"] = "DSH"
    tools.append(ds)
    z = zcode_stats(since)
    z["group"] = "ZCode"
    tools.append(z)
    pi = pi_stats(since)
    tools.append(pi)
    ag = antigravity_status()
    ag["group"] = "Antigravity"
    # 真实 token 记账（LS RPC）—— 与配额并存，口径各自标注
    agt = antigravity_tokens(since=since)
    if agt.get("available"):
        ag["tokens"] = agt
        ag["available"] = True
        ag["kind"] = "tokens+quota" if ag.get("groups") else "tokens"
        ag["requests"] = agt.get("requests", 0)
        ag["input"] = agt.get("input", 0)
        ag["output"] = agt.get("output", 0)
        ag["cache_read"] = agt.get("cache_read", 0)
        ag["cache_write"] = 0
        ag["total_tokens"] = agt.get("total_tokens", 0)
        ag["cache_hit_rate"] = agt.get("cache_hit_rate", 0.0)
        ag["models"] = agt.get("models", [])
        ag["sessions"] = agt.get("sessions", 0)
        ag["cost_usd"] = None
    else:
        ag["tokens_unavailable"] = agt.get("reason")
    tools.append(ag)
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "since": since, "tools": tools}


# ---------------------------------------------------------------- 实时脉搏（仪表盘 Zen Pulse 页）
# CCS 侧只动 proxy_request_logs 单表（逐请求行天然带时间戳，含 data_source='session_log'
# 的会话同步行），全部是带 WHERE 下界的 GROUP BY / LIMIT，毫秒级返回；
# 其余本机 agent 经 _agent_recent_events 逐事件并入（60 秒缓存）。
# 与 ccs_daily 相同的口径约定：input 折算纯口径；total = 纯input+output+cache_read+cache_creation。

def _zp_row_tokens(sem, inp, out, cr, cc):
    """单行请求的 token 总量（纯口径）。"""
    return _pure_input(sem, inp, cr, cc) + (out or 0) + (cr or 0) + (cc or 0)


# ---------------------------------------------------------------- Claude 本机直读（绕过 CCS）
# 数据源：~/.claude/projects/**/*.jsonl（Claude Code CLI / 桌面端 Cowork 的会话日志）。
# 每条 assistant 行自带毫秒级 timestamp 与完整 usage，是 CC 用量的权威记账；
# CCS 的 session_log 同步行是同一批会话的第二份记账，直接用会双算。
# entrypoint 字段区分发起端：'claude-desktop'（桌面端）/ 'sdk-cli'（CLI）。
#
# 两个必须的处理（2026-10-08 实测本机 300 文件 / 406MB / 3.9 万行）：
#   1) 去重：同一 assistant 消息在会话续写、分支/子代理复制时会写进多个 jsonl，
#      实测 3.9 万行里有 2.5 万行是同 message.id 的副本（桌面端被放大约 2.3 倍）。
#      按 message.id 全局去重后与 CCS 会话同步量基本吻合。
#   2) 不限 14 天：原实现只扫近 14 天写入的文件，时间范围选 30 天/全部时
#      Claude 用量被截断（环形图与工具卡都偏小）。现改为全量扫描 + per-file
#      增量缓存，窗口过滤交给调用方。
_CLAUDE_NATIVE_CACHE = {"rows": None, "files": {}}
_CLAUDE_NATIVE_BG = {"started": False}
_CLAUDE_NATIVE_LOCK = threading.Lock()
_WARM = {"started": False}


def _claude_native_parse(path):
    """解析单个 jsonl，返回逐条 API 调用（含去重键 id）。"""
    per = []
    try:
        # 二进制读 + bytes 子串预筛：406MB 全量重扫比文本模式快约 40%
        # （只有命中 usage 的行才 json.loads，bytes 同样可直接解析）。
        fh = open(path, "rb")
    except OSError:
        return per
    with fh:
        for ln in fh:
            if b'"usage"' not in ln or b'"assistant"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage") or {}
            if not u:
                continue
            inp = u.get("input_tokens") or 0
            outp = u.get("output_tokens") or 0
            cr = u.get("cache_read_input_tokens") or 0
            cc = u.get("cache_creation_input_tokens") or 0
            if not (inp or outp or cr or cc):
                # 2026-10-13：无 usage 的占位行（<synthetic> 等，token 全 0，
                # 通常是错误/系统合成消息）不计请求——此前只有模型来源表跳过，
                # 分钟桶/请求流等消费端会把它们当一行请求并出现在 v-t 模型清单里。
                continue
            ts = _claude_iso_ts(d.get("timestamp"))
            if ts is None:
                continue
            per.append({
                "ts": ts,
                "id": msg.get("id") or d.get("uuid") or "",
                "model": msg.get("model") or "",
                "input": inp,
                "output": outp,
                "cache_read": cr,
                "cache_creation": cc,
                "session": d.get("sessionId") or "",
                "entrypoint": str(d.get("entrypoint") or "sdk-cli"),
            })
    return per


def _claude_native_scan(since_ts=None):
    """扫描本机 Claude 会话日志（per-file 增量缓存 + 按消息 id 去重）。

    since_ts 只决定「本轮补扫哪些文件」（按文件 mtime 取窗口），返回的始终是
    per-file 缓存里已有的全部行——选 7 天窗口时不必解析几个月前的历史文件
    （本机 300 文件 / 406MB，全量冷扫约 3s；窗口内提速约 2~3 倍）。
    调用方必须持有 _CLAUDE_NATIVE_LOCK：per-file 缓存是跨线程共享的。
    """
    root = os.path.expanduser(r"~\.claude\projects")
    files = []
    if os.path.isdir(root):
        for dirpath, _dirs, names in os.walk(root):
            for nm in names:
                if not nm.endswith(".jsonl"):
                    continue
                fp = os.path.join(dirpath, nm)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_size > 0:
                    files.append((fp, st.st_size, int(st.st_mtime)))
    files.sort()
    cache_files = _CLAUDE_NATIVE_CACHE.setdefault("files", {})
    merged = {}
    for fp, sz, mt in files:
        hit = cache_files.get(fp)
        if hit and hit[0] == (sz, mt):
            per = hit[1]
        elif since_ts and mt < since_ts:
            continue            # 窗口外：不解析（缓存留着，切回大窗口还能复用）
        else:
            per = _claude_native_parse(fp)
            cache_files[fp] = ((sz, mt), per)
        for row in per:
            key = row["id"] or f"{row['session']}:{row['ts']}:{row['model']}"
            if key not in merged:
                merged[key] = row
    live = {fp for fp, _sz, _mt in files}
    for fp in [k for k in cache_files if k not in live]:
        cache_files.pop(fp, None)   # 已删除的会话文件不留缓存
    return sorted(merged.values(), key=lambda r: r["ts"])


def _claude_native_bg_loop():
    """后台每 12s 热刷新 native 缓存——扫描不占请求线程，
    pulse 轮询永远直接读现成缓存（活跃会话持续写 jsonl）。

    首次全量补齐推迟 25s：冷启动时先让窗口内请求走快路径，
    否则全量解析会和首屏抢 GIL（Python 线程 CPU 密集任务串行执行）。
    """
    time.sleep(25)
    while True:
        try:
            with _CLAUDE_NATIVE_LOCK:
                rows = _claude_native_scan()
                _CLAUDE_NATIVE_CACHE["rows"] = rows
        except Exception:
            pass
        time.sleep(12)


def _claude_native_bg_start():
    if _CLAUDE_NATIVE_BG["started"]:
        return
    _CLAUDE_NATIVE_BG["started"] = True
    threading.Thread(target=_claude_native_bg_loop, daemon=True,
                     name="claude-native-scan").start()


def _claude_iso_ts(v):
    """ISO8601（含 Z/毫秒）→ epoch 秒。"""
    if not v:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _claude_native_rows(since_ts=None):
    """本机 Claude 逐条 API 调用（已按消息 id 去重）。

    since_ts 只影响「本轮补扫哪些文件」（窗口内快路径）；返回值是 per-file
    缓存里已有的全部行，调用方各自按自己的窗口过滤。全量缓存由后台线程
    维护（启动 25s 后补齐），期间每次调用走窗口内增量扫描。
    """
    rows = _CLAUDE_NATIVE_CACHE.get("rows")
    if rows is not None:
        return rows
    with _CLAUDE_NATIVE_LOCK:
        if _CLAUDE_NATIVE_CACHE.get("rows") is not None:
            return _CLAUDE_NATIVE_CACHE["rows"]
        return _claude_native_scan(since_ts)


def claude_native_stats(since_ts=None):
    """Claude 本机用量聚合（CC 卡权威口径；成本由 CCS 估算另行提供）。"""
    rows = [r for r in _claude_native_rows(since_ts)
            if since_ts is None or r["ts"] >= since_ts]
    agg = {
        "requests": len(rows),
        "sessions": len({r["session"] for r in rows if r["session"]}),
        "input": sum(r["input"] for r in rows),
        "output": sum(r["output"] for r in rows),
        "cache_read": sum(r["cache_read"] for r in rows),
        "cache_creation": sum(r["cache_creation"] for r in rows),
    }
    agg["total_tokens"] = (agg["input"] + agg["output"]
                           + agg["cache_read"] + agg["cache_creation"])
    return agg


def claude_model_source(since_ts=None):
    """Claude 本机直读来源（CLI + 桌面端），供模型来源注册表使用。

    CCS 的 claude 会话同步行是同一批会话的第二份记账，已在 ccs_models_full
    里剔除；桌面端走代理的聊天行不落 jsonl，仍算在 CCS 来源里。
    无 usage 的占位行（<synthetic> 等，token 全 0）不计请求，避免模型表出现
    0 份额的空行。
    """
    models = {}
    for r in _claude_native_rows(since_ts):
        if since_ts and r["ts"] < since_ts:
            continue
        inp, outp = int(r["input"]), int(r["output"])
        cr, cc = int(r["cache_read"]), int(r["cache_creation"])
        if not (inp or outp or cr or cc):
            continue
        _add_model_usage(models, r["model"] or "(unknown)", requests=1,
                         input=inp, output=outp, cache_read=cr,
                         cache_creation=cc, cache_denominator=inp + cr + cc)
    return _source_result(
        "claude", models,
        note="本机 ~/.claude/projects 逐条记账，已按消息 id 去重；"
             "entrypoint 区分 CLI 与桌面端，二者合并在本来源。")


# ---------------------------------------------------------------- DSH（DeepSeek Harness）
# 数据源：~/.dsh/sessions/<workspace>/<session-id>/session.v4.jsonl.zstd
# 该文件是「一条记录一个 zstd frame」的追加流（单会话实测 1000+ 帧），必须
# read_across_frames=True 一次读完，否则只解出第一帧（=会话元信息）。
# 每条 assistant/message 自带 usage：
#   inputTokens / outputTokens / cacheReadTokens / cacheWriteTokens / totalTokens
# 语义：totalTokens = input + output + cacheRead + cacheWrite，其中 input 是纯
# input、缓存读单列——与项目「纯 input」口径一致，不需要再折算。
# 模型名取该消息之前最近一条 model/selection；会话开头的记录回填该会话首个模型。
DSH_SESSION_GLOB = os.path.expanduser(
    os.path.join("~", ".dsh", "sessions", "*", "*", "session*.jsonl.zstd"))
_DSH_MEM = {"files": {}}
_DSH_LOCK = threading.Lock()
_DSH_ZSTD = {"tried": False, "mod": None}


def _dsh_zstd():
    """延迟加载 zstandard：缺依赖时只有 DSH 来源标记不可用，不影响其他来源。"""
    if not _DSH_ZSTD["tried"]:
        _DSH_ZSTD["tried"] = True
        try:
            import zstandard
            _DSH_ZSTD["mod"] = zstandard
        except Exception:
            _DSH_ZSTD["mod"] = None
    return _DSH_ZSTD["mod"]


def _dsh_parse_file(path):
    """解析单个 DSH 会话文件 → [(ts 秒, 模型, 纯input, output, cache_read, cache_write, 会话名, 延迟ms)]。"""
    zstd = _dsh_zstd()
    if zstd is None:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    if not raw:
        return []
    try:
        text = zstd.ZstdDecompressor().stream_reader(
            io.BytesIO(raw), read_across_frames=True).read()
    except Exception:
        return []
    session = os.path.basename(os.path.dirname(path))
    rows = []
    models = []
    cur_model = ""
    for ln in text.decode("utf-8", "replace").split("\n"):
        if ('"assistant/message"' not in ln and
            '"model/selection"' not in ln and
            '"request/context"' not in ln and
            '"session/title-llm-request"' not in ln):
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        kind = rec.get("type")
        if kind in ("model/selection", "request/context"):
            m = (rec.get("data") or {}).get("model")
            if m:
                cur_model = m
                if cur_model not in models:
                    models.append(cur_model)
        elif kind == "session/title-llm-request":
            m = (rec.get("data") or {}).get("route", {}).get("model")
            if m and m not in models:
                models.append(m)
        elif kind == "assistant/message":
            d = rec.get("data") or {}
            usage = d.get("usage") or {}
            if not usage:
                continue
            ts = _timestamp_epoch(rec.get("time"))
            if not ts:
                continue
            msg_model = (d.get("message") or {}).get("source", {}).get("model")
            if msg_model:
                cur_model = msg_model
                if cur_model not in models:
                    models.append(cur_model)
            model_to_use = msg_model or cur_model
            stream = d.get("stream") or []
            times = [c["time"] for c in stream if isinstance(c, dict) and "time" in c]
            lat_ms = int(max(times) - min(times)) if len(times) >= 2 else 0
            rows.append((ts, model_to_use,
                         int(usage.get("inputTokens") or 0),
                         int(usage.get("outputTokens") or 0),
                         int(usage.get("cacheReadTokens") or 0),
                         int(usage.get("cacheWriteTokens") or 0), session, lat_ms))
    first_model = models[0] if models else "deepseek-chat"
    rows = [(ts, m or first_model, i, o, cr, cw, s, lat)
            for ts, m, i, o, cr, cw, s, lat in rows]
    return rows


def _dsh_rows(since_ts=None):
    """DSH 逐条 API 调用（per-file (size, mtime) 增量缓存，与 Claude/Codex 同套路）。

    返回 None 表示本机没有 zstandard 解码库（调用方据此标记来源不可用）。
    """
    if _dsh_zstd() is None:
        return None
    cache = _DSH_MEM["files"]
    live = set()
    for path in glob.glob(DSH_SESSION_GLOB):
        try:
            st = os.stat(path)
        except OSError:
            continue
        live.add(path)
        if since_ts and st.st_mtime < since_ts:
            continue            # 窗口外：不解析（缓存留着，切回大窗口还能复用）
        key = (st.st_size, int(st.st_mtime), 2)
        with _DSH_LOCK:
            hit = cache.get(path)
            if hit and hit[0] == key:
                continue
        rows = _dsh_parse_file(path) or []
        with _DSH_LOCK:
            cache[path] = (key, rows)
    with _DSH_LOCK:
        for p in [k for k in cache if k not in live]:
            cache.pop(p, None)
        out = []
        for item in cache.values():
            if since_ts and item[0][1] < since_ts:
                continue
            out.extend(item[1])
    return out


def dsh_stats(since_ts=None):
    """DSH（DeepSeek Harness）工具卡数据。"""
    rows = _dsh_rows(since_ts)
    if rows is None:
        return {"name": "DSH", "group": "DSH", "available": False,
                "reason": "未安装 zstandard 解码库，无法读取 DSH 会话"}
    rows = [r for r in rows if not since_ts or r[0] >= since_ts]
    if not rows:
        return {"name": "DSH", "group": "DSH", "available": False,
                "reason": "未发现 DSH 会话记账（~/.dsh/sessions）"}
    inp = sum(r[2] for r in rows)
    outp = sum(r[3] for r in rows)
    cr = sum(r[4] for r in rows)
    cw = sum(r[5] for r in rows)
    denom = inp + cr + cw
    models = {}
    for r in rows:
        row = models.setdefault(r[1] or "(unknown)", {
            "model": r[1] or "(unknown)", "requests": 0, "input": 0,
            "output": 0, "cache_read": 0, "cache_write": 0, "total": 0})
        row["requests"] += 1
        row["input"] += r[2]
        row["output"] += r[3]
        row["cache_read"] += r[4]
        row["cache_write"] += r[5]
        row["total"] += r[2] + r[3] + r[4] + r[5]
    return {
        "name": "DSH", "group": "DSH", "available": True,
        "source": "dsh:sessions",
        "sessions": len({r[6] for r in rows}), "requests": len(rows),
        "input": inp, "output": outp, "cache_read": cr, "cache_write": cw,
        "total_tokens": inp + outp + cr + cw,
        "cache_hit_rate": round(cr / denom, 4) if denom else 0.0,
        "models": sorted(models.values(), key=lambda m: -m["total"]),
        "cost_usd": None,
        "note": "DSH 会话文件（~/.dsh/sessions）逐条 assistant/message usage；"
                "input 为纯 input、缓存读单列；本机无计价，成本不显示。",
    }


def dsh_model_source(since_ts=None):
    """DSH 模型来源：~/.dsh/sessions 逐条 usage。"""
    rows = _dsh_rows(since_ts)
    if rows is None:
        return _missing_source("dsh", "未安装 zstandard 解码库，无法读取 DSH 会话")
    models = {}
    for r in rows:
        ts = r[0]
        if since_ts and ts < since_ts:
            continue
        model = r[1] or "(unknown)"
        inp, outp, cr, cw = r[2], r[3], r[4], r[5]
        _add_model_usage(models, model, requests=1, input=inp,
                         output=outp, cache_read=cr, cache_write=cw,
                         cache_denominator=inp + cr + cw)
    return _source_result(
        "dsh", models,
        note="本机 ~/.dsh/sessions 逐条 assistant/message usage；"
             "input 为纯 input（缓存读单列），模型名取会话内 request/context 或 model/selection。")


def pi_stats(since_ts=None):
    """Pi / OMP 本机会话原生用量统计（~/.pi 与 ~/.omp）。

    读取 assistant usage 与 compaction 事件，计算 token、成本与模型分布。
    """
    paths = _pi_session_paths()
    if not paths and not _pi_session_dirs():
        return {
            "name": "Pi / OMP", "app_type": "pi", "group": "Pi",
            "available": False, "reason": "未发现 Pi / OMP 本机会话目录（~/.pi 或 ~/.omp）",
            "requests": 0, "sessions": 0, "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0, "cache_creation": 0,
            "total_tokens": 0, "cache_hit_rate": 0.0, "cost_usd": None
        }
    reqs = inp = outp = cr = cw = 0
    total_cost = 0.0
    has_cost = False
    active_sessions = set()
    models = {}
    for path in paths:
        try:
            if since_ts and os.path.getmtime(path) < since_ts:
                continue
        except OSError:
            continue
        sess_has_msg = False
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip() or ('"usage"' not in line and '"tokens"' not in line):
                        continue
                    try:
                        entry = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    kind = entry.get("type")
                    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                    if kind == "message":
                        if message.get("role") != "assistant":
                            continue
                        usage = message.get("usage") or entry.get("usage")
                    elif kind in ("usage", "compaction", "branch_summary"):
                        usage = entry.get("usage") or entry.get("tokens") or message.get("usage")
                    else:
                        continue
                    if not isinstance(usage, dict):
                        continue
                    ts = _record_timestamp(
                        message.get("timestamp"), entry.get("timestamp"),
                        message.get("time"), entry.get("time"))
                    if not _in_window(ts, since_ts):
                        continue
                    v = _usage_fields(usage)
                    u_inp = v.get("input", 0)
                    u_out = v.get("output", 0) + v.get("reasoning", 0)
                    u_cr = v.get("cache_read", 0)
                    u_cw = v.get("cache_write", 0)
                    u_tot = v.get("total_tokens") or (u_inp + u_out + u_cr + u_cw)

                    reqs += 1
                    inp += u_inp
                    outp += u_out
                    cr += u_cr
                    cw += u_cw
                    sess_has_msg = True

                    cost_obj = usage.get("cost")
                    if isinstance(cost_obj, dict) and "total" in cost_obj:
                        total_cost += float(cost_obj.get("total") or 0.0)
                        has_cost = True
                    elif usage.get("cost_usd") is not None:
                        total_cost += float(usage.get("cost_usd") or 0.0)
                        has_cost = True

                    mdl = (message.get("model") or message.get("modelID")
                           or entry.get("model") or entry.get("modelID") or "(unknown)")
                    mrow = models.setdefault(mdl, {
                        "model": mdl, "requests": 0, "input": 0,
                        "output": 0, "cache_read": 0, "cache_write": 0, "total": 0
                    })
                    mrow["requests"] += 1
                    mrow["input"] += u_inp
                    mrow["output"] += u_out
                    mrow["cache_read"] += u_cr
                    mrow["cache_write"] += u_cw
                    mrow["total"] += u_tot
        except OSError:
            continue
        if sess_has_msg:
            active_sessions.add(path)

    tot_tokens = inp + outp + cr + cw
    denom = inp + cr + cw
    hit_rate = round(cr / denom, 4) if denom > 0 else 0.0
    models_list = sorted(models.values(), key=lambda x: -x["total"])

    return {
        "name": "Pi / OMP",
        "app_type": "pi",
        "group": "Pi",
        "available": True if (reqs > 0 or not since_ts) else False,
        "source": "pi+omp:sessions",
        "requests": reqs,
        "sessions": len(active_sessions),
        "input": inp,
        "output": outp,
        "cache_read": cr,
        "cache_write": cw,
        "cache_creation": 0,
        "total_tokens": tot_tokens,
        "cache_hit_rate": hit_rate,
        "models_count": len(models),
        "models": models_list,
        "cost_usd": round(total_cost, 4) if has_cost else None,
        "note": "本机 Pi 及 OMP 会话原生记账（~/.pi 与 ~/.omp）。"
    }


def warm_caches():
    """服务启动后在后台预热本机扫描，首屏请求不再等冷扫描。

    Claude jsonl 由 12s 常驻线程维护（首次全量补齐推迟 25s，先让首屏窗口内
    请求走快路径）；Codex rollout 每 20s 走一次增量扫描（未变的文件只 stat，
    选「全部」时所需的旧文件也在后台逐步补上）。重复调用无副作用。
    """
    if _WARM["started"]:
        return
    _WARM["started"] = True
    _claude_native_bg_start()

    def _loop():
        time.sleep(20)          # 同上：错开首屏，避免与窗口内解析抢 GIL
        while True:
            try:
                _codex_scan(None)   # 全窗口：后台补齐历史文件
            except Exception:
                pass
            time.sleep(20)

    threading.Thread(target=_loop, daemon=True, name="codex-warm").start()


# ---------------------------------------------------------------- 多 Agent 逐事件（pulse 分钟/小时桶并入）
# pulse 的 token 计数口径（分钟桶 / 小时桶 / 5 小时窗口）原先只并 CCS + Hermes +
# ZCode + claude-native，漏掉本机其余 agent。这里按逐事件收集：
#   Codex rollout（全线路权威记账，走 CCS/Codex++/官方直连都写它）
#   Pi 本地会话 jsonl、OpenCode / MiMo-Code（同构 SQLite message 库）、
#   WorkBuddy traces、CommandCode（NEW-API 3050 渠道逐行）。
# 防双算：CCS 的 codex / pi 行是同一批流量的第二份记账（proxy 期流量 +
# codex_session / pi_session 会话同步副本），pulse 的 CCS 查询同步剔除。
# Antigravity 只有日期粒度（LS RPC），无法落时间桶，不在此列。
# 事件元组：(ts 秒, 纯input, output, cache_read, cache_creation)；成本不可得计 0。
_AGENT_RECENT_MEM = {"ts": 0.0, "events": []}
_AGENT_RECENT_TTL = 60.0


def _codex_event_rows(since_ts):
    """Codex rollout 逐 token_count 事件（OpenAI 语义 input 已含 cached，
    折算纯口径；个别 provider cache 单列时 cached>input，与 _codex_models 同法）。

    与 _codex_scan 共用同一份 per-file 解析缓存：同一批 rollout 文件只读一遍，
    pulse 冷启动不再为事件流二次解析全量文件。
    """
    events = []
    for _path, item in _codex_scan(since_ts).items():
        if item[3]:
            events.extend(item[3])
    return events


def _pi_event_rows(since_ts):
    """Pi / OMP 本地会话（~/.pi 与 ~/.omp）逐 assistant/usage 事件。"""
    paths = _pi_session_paths()
    if not paths:
        return []
    events = []
    for path in paths:
        try:
            if os.path.getmtime(path) < since_ts:
                continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line and '"tokens"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    kind = entry.get("type")
                    message = (entry.get("message")
                               if isinstance(entry.get("message"), dict) else {})
                    if kind == "message":
                        if message.get("role") != "assistant":
                            continue
                        usage = message.get("usage") or entry.get("usage")
                    elif kind in ("usage", "compaction", "branch_summary"):
                        usage = entry.get("usage") or entry.get("tokens") or message.get("usage")
                    else:
                        continue
                    if not isinstance(usage, dict):
                        continue
                    ts = _record_timestamp(
                        message.get("timestamp"), entry.get("timestamp"),
                        message.get("time"), entry.get("time"))
                    if not ts or ts < since_ts:
                        continue
                    v = _usage_fields(usage)
                    mdl = (message.get("model") or message.get("modelID")
                           or entry.get("model") or entry.get("modelID") or "pi")
                    cost_val = 0.0
                    cost_obj = usage.get("cost")
                    if isinstance(cost_obj, dict) and "total" in cost_obj:
                        cost_val = float(cost_obj.get("total") or 0.0)
                    elif usage.get("cost_usd") is not None:
                        cost_val = float(usage.get("cost_usd") or 0.0)
                    events.append((ts, v.get("input", 0),
                                   v.get("output", 0) + v.get("reasoning", 0),
                                   v.get("cache_read", 0), v.get("cache_write", 0),
                                   mdl, cost_val))
        except OSError:
            continue
    return events


def _json_db_event_rows(path, since_ts):
    """OpenCode / MiMo-Code 同构 SQLite message 库的 assistant 用量事件。"""
    if not (path and os.path.isfile(path)):
        return []
    events = []
    try:
        for raw_time, role, _name, raw_tokens, raw_usage, message_time, timestamp, _provider \
                in _sqlite_data_rows(path):
            if role != "assistant":
                continue
            tokens = _json_object(raw_tokens) or _json_object(raw_usage)
            if not tokens:
                continue
            ts = _record_timestamp(message_time, raw_time, timestamp)
            if not ts or ts < since_ts:
                continue
            v = _usage_fields(tokens)
            events.append((ts, v.get("input", 0),
                           v.get("output", 0) + v.get("reasoning", 0),
                           v.get("cache_read", 0), v.get("cache_write", 0)))
    except Exception:
        return events
    return events


def _workbuddy_event_rows(since_ts):
    """WorkBuddy traces（文件级时间戳，粒度=一次 trace）。"""
    if not os.path.isdir(WORKBUDDY_TRACE_DIR):
        return []
    events = []
    for path in glob.glob(os.path.join(WORKBUDDY_TRACE_DIR, "**", "trace_*.json"),
                          recursive=True):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                trace = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(trace, dict):
            continue
        ts = _record_timestamp(
            trace.get("timestamp"), trace.get("createdAt"), trace.get("created_at"),
            trace.get("startTime"), trace.get("time"))
        if not ts or ts < since_ts:
            continue
        for _name, fields in _workbuddy_trace_models(trace):
            inp, outp = fields.get("input", 0), fields.get("output", 0)
            cr, cc = fields.get("cache_read", 0), fields.get("cache_write", 0)
            if not (inp + outp + cr + cc) and fields.get("total_tokens"):
                inp = fields["total_tokens"]  # 只有总量时整体落 input
            events.append((ts, inp, outp + fields.get("reasoning", 0), cr, cc))
    return events



def _agent_recent_events(since_ts):
    """各 agent 逐事件用量（脉冲桶共用；60 秒缓存，固定收集 49 小时窗）。"""
    now = time.time()
    if _AGENT_RECENT_MEM["events"] and now - _AGENT_RECENT_MEM["ts"] < _AGENT_RECENT_TTL:
        return [e for e in _AGENT_RECENT_MEM["events"] if e[0] >= since_ts]
    horizon = now - 49 * 3600
    events = []
    events.extend(_codex_event_rows(horizon))
    events.extend(_pi_event_rows(horizon))
    opencode_db = next((p for p in _opencode_db_candidates() if os.path.isfile(p)), None)
    events.extend(_json_db_event_rows(opencode_db, horizon))
    mimo_db = next((p for p in _mimocode_db_candidates() if os.path.isfile(p)), None)
    events.extend(_json_db_event_rows(mimo_db, horizon))
    events.extend(_workbuddy_event_rows(horizon))
    _AGENT_RECENT_MEM.update({"ts": now, "events": events})
    return [e for e in events if e[0] >= since_ts]


def _session_spread(t0, t1, step, grid0, nbuckets):
    """会话 [t0, t1] 在 [grid0, grid0+nbuckets*step) 各时间桶上的占比列表。

    2026-10-13：Hermes 会话累计值此前按会话开始时刻一次性计入单个桶——正在
    使用的会话在 v-t/堆叠图里实时不可见（当前 v 恒 0），开始时刻滑出窗口还会
    整段消失。改为按活跃区间 [started_at, last_activity_at] 时间均摊（无逐调用
    台账，近似口径；会话完整落在窗内时占比之和 = 1，累计值守恒；跨窗只取窗内
    部分）。t1 缺失时按 t0+60s 单点会话处理。返回 [(桶下标, 占比), ...]。
    """
    t0 = float(t0 or 0)
    t1 = float(t1 or 0)
    if t1 <= t0:
        t1 = t0 + 60.0
    span = t1 - t0
    res = []
    lo = max(t0, float(grid0))
    hi = min(t1, float(grid0 + nbuckets * step))
    if hi <= lo:
        return res
    b = int((lo - grid0) // step)
    while b < nbuckets:
        b_start = grid0 + b * step
        ov = min(b_start + step, hi) - max(b_start, lo)
        if ov > 0:
            res.append((b, ov / span))
        if b_start + step >= hi:
            break
        b += 1
    return res


# v-t 图时间筛选（2026-10-13）：range_min -> 桶宽（秒）白名单。
# 桶数 = ceil(range_min*60 / bucket_sec) ≤ 180；60/60 即 pulse 的分钟桶口径。
VT_RANGES = {60: 60, 180: 60, 360: 120, 720: 240, 1440: 480}


def _rate_series(now, range_min=60, bucket_sec=60):
    """滚动窗口 [now - range_min*60, now] 的速率桶序列（pulse 与 v-t 时间筛选共用）。

    口径与 pulse() 完全一致：CCS 主表剔除 claude/claude-desktop 会话同步行与
    codex/pi 行后按桶分组；其余本机 agent（Hermes 会话 / ZCode / claude-native /
    DSH / Codex / Pi / OpenCode / MiMo / WorkBuddy / CommandCode / Antigravity）
    逐事件并入（并入理由见各来源注释，剔除规则同上）；input 按
    input_token_semantics 折算纯口径，多来源在 Python 侧合并。
    返回 {"minutes": [...], "minutes_by_model": {...}, "models": [...]}，每桶字段
    与 pulse 分钟桶一致（requests/tokens/output_tokens/cost/by_model），桶数
    n = ceil(range_min*60 / bucket_sec) ≤ 180；bucket_sec=60 时与历史行为逐字段一致。
    """
    if not os.path.exists(CCS_DB):
        return None
    n = -(-range_min * 60 // bucket_sec)          # ceil：窗口桶数（≤180）
    cur_b = now // bucket_sec
    since = now - range_min * 60
    minutes = {}
    con = _ro(CCS_DB)
    try:
        cur = con.cursor()
        for r in cur.execute(
                """SELECT created_at/?, model, COUNT(*),
                          SUM(input_tokens), SUM(output_tokens),
                          SUM(cache_read_tokens), SUM(cache_creation_tokens),
                          input_token_semantics, SUM(CAST(total_cost_usd AS REAL))
                   FROM proxy_request_logs WHERE created_at >= ?
                   AND NOT (app_type = 'claude'
                        OR app_type IN ('codex', 'pi')
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   GROUP BY created_at/?, model, input_token_semantics""",
                (bucket_sec, since, bucket_sec)):
            e = minutes.setdefault(r[0], {"requests": 0, "tokens": 0, "output_tokens": 0, "cost": 0.0, "by_model": {}})
            reqs = r[2] or 0
            toks = (_pure_input(r[7], r[3], r[5], r[6]) + (r[4] or 0)
                    + (r[5] or 0) + (r[6] or 0))
            out_toks = r[4] or 0
            e["requests"] += reqs
            e["tokens"] += toks
            e["output_tokens"] = e.get("output_tokens", 0) + out_toks
            e["cost"] += r[8] or 0.0
            mdl = r[1] or ""
            if mdl:
                me = e["by_model"].setdefault(mdl, {"requests": 0, "tokens": 0, "output_tokens": 0})
                me["requests"] += reqs
                me["tokens"] += toks
                me["output_tokens"] = me.get("output_tokens", 0) + out_toks
    finally:
        con.close()

    def _minutes_bump(ts, inp, outp, cr, cc, cost, reqs=1, model=None):
        mb = int(ts or 0) // bucket_sec
        if mb < cur_b - (n - 1) or mb > cur_b:
            return
        e = minutes.setdefault(mb, {"requests": 0, "tokens": 0, "output_tokens": 0, "cost": 0.0, "by_model": {}})
        toks = (inp or 0) + (outp or 0) + (cr or 0) + (cc or 0)
        e["requests"] += reqs
        e["tokens"] += toks
        e["output_tokens"] = e.get("output_tokens", 0) + (outp or 0)
        e["cost"] += cost or 0.0
        if model:
            me = e["by_model"].setdefault(model, {"requests": 0, "tokens": 0, "output_tokens": 0, "cost": 0.0})
            me["requests"] += reqs
            me["tokens"] += toks
            me["output_tokens"] = me.get("output_tokens", 0) + (outp or 0)
            me["cost"] += cost or 0.0

    for hname in _active_agents():
        hdb = _agent_db(hname)
        if not os.path.exists(hdb):
            continue
        try:
            hcon = _ro(hdb)
            # 2026-10-13：会话累计值按活跃区间分摊到分钟桶（原实现挤在
            # started_at 那一分钟，正在用的会话实时不可见）；请求数仍记在
            # 会话启动所在桶（「一波 = 一次会话启动」口径不变）。
            lo_b = cur_b - (n - 1)
            for hr in hcon.execute(
                    """SELECT started_at, last_activity_at, ended_at, model,
                              input_tokens, output_tokens,
                              cache_read_tokens, cache_write_tokens,
                              estimated_cost_usd
                       FROM sessions
                       WHERE started_at <= ?
                         AND COALESCE(last_activity_at, ended_at, started_at) >= ?""",
                    (now, since)):
                t0 = hr[0] or 0
                t1 = min(hr[1] or hr[2] or hr[0] or 0, now)
                cost = hr[8] or 0.0
                if cost > COST_SANE_LIMIT:
                    cost = 0.0
                b0 = int(t0 // bucket_sec)
                for b, frac in _session_spread(t0, t1, bucket_sec, lo_b * bucket_sec, n):
                    abs_b = lo_b + b
                    _minutes_bump(abs_b * bucket_sec + 1, (hr[4] or 0) * frac,
                                  (hr[5] or 0) * frac, (hr[6] or 0) * frac,
                                  (hr[7] or 0) * frac, cost * frac,
                                  reqs=(1 if abs_b == b0 else 0), model=hr[3])
            hcon.close()
        except Exception:
            pass
    try:
        zdb = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
        if os.path.exists(zdb):
            zcon = _ro(zdb)
            for zr in zcon.execute(
                    """SELECT started_at/1000, model_id, input_tokens, output_tokens,
                              cache_read_input_tokens,
                              cache_creation_input_tokens
                       FROM model_usage WHERE started_at >= ?""",
                    (since * 1000,)):
                _minutes_bump(zr[0], zr[2], zr[3], zr[4], zr[5], 0.0, model=zr[1])
            zcon.close()
    except Exception:
        pass
    try:
        for nr in _claude_native_rows(now - 14 * 86400):
            if nr["ts"] >= since:
                _minutes_bump(nr["ts"], nr["input"], nr["output"],
                              nr["cache_read"], nr["cache_creation"], 0.0, model=nr.get("model"))
    except Exception:
        pass
    # DSH 本机会话逐条并入（同一套 zstd 解析缓存，见 _dsh_rows）
    try:
        for dr in _dsh_rows(now - 14 * 86400) or []:
            if dr[0] >= since:
                _minutes_bump(dr[0], dr[2], dr[3], dr[4], dr[5], 0.0, model=dr[1])
    except Exception:
        pass
    # 其余本机 agent 逐事件并入（Codex rollout 全线路 / Pi / OpenCode /
    # MiMo / WorkBuddy / CommandCode）
    try:
        for ev in _agent_recent_events(since):
            mdl = ev[5] if len(ev) > 5 else None
            _minutes_bump(ev[0], ev[1], ev[2], ev[3], ev[4], 0.0, model=mdl)
    except Exception:
        pass
    # Antigravity 本机逐条并入
    try:
        for ar in _antigravity_native_rows(since):
            _minutes_bump(ar["ts"], ar["input"], ar["output"],
                          ar["cache_read"], ar["cache_creation"], 0.0, model=ar.get("model"))
    except Exception:
        pass
    minutes_out = [minutes.get(cur_b - i,
                               {"requests": 0, "tokens": 0, "output_tokens": 0, "cost": 0.0, "by_model": {}})
                   for i in range(n - 1, -1, -1)]
    active_models = set()
    for m in minutes_out:
        for mdl in m.get("by_model", {}):
            if mdl:
                active_models.add(mdl)
    minutes_by_model = {}
    for mdl in sorted(active_models):
        series_m = []
        for m in minutes_out:
            me = m.get("by_model", {}).get(mdl, {"requests": 0, "tokens": 0, "output_tokens": 0, "cost": 0.0})
            series_m.append(me)
        minutes_by_model[mdl] = series_m
    return {"minutes": minutes_out, "minutes_by_model": minutes_by_model,
            "models": sorted(active_models)}


def rate_series(range_min=60):
    """v-t 图时间筛选：指定滚动窗口的速率序列（非默认窗口独立取数）。

    range_min 白名单见 VT_RANGES（60/180/360/720/1440 分钟，桶宽自适应，桶数 ≤180），
    非法值回退 60。返回结构含 pulse.minutes/minutes_by_model 同形数据，另有
    range_min/bucket_sec 供前端积分换算（v_i = 桶总量 / bucket_sec）。
    """
    now = int(time.time())
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "now": now}
    bucket_sec = VT_RANGES.get(int(range_min))
    if bucket_sec is None:
        range_min, bucket_sec = 60, 60
    out["range_min"] = range_min
    out["bucket_sec"] = bucket_sec
    if not os.path.exists(CCS_DB):
        out["available"] = False
        out["reason"] = "未找到 CCS 数据库"
        out["minutes"] = []
        out["minutes_by_model"] = {}
        out["models"] = []
        return out
    out["available"] = True
    _claude_native_bg_start()
    try:
        rs = _rate_series(now, range_min, bucket_sec) or {}
        out["minutes"] = rs.get("minutes", [])
        out["minutes_by_model"] = rs.get("minutes_by_model", {})
        out["models"] = rs.get("models", [])
    except Exception as e:
        out["available"] = False
        out["reason"] = str(e)
        out["minutes"] = []
        out["minutes_by_model"] = {}
        out["models"] = []
    return out


def pulse():
    """仪表盘「实时脉搏」聚合：速率 / 小时堆叠 / 请求波 / 请求流 / 工具日序列 / 缓存节省。

    只动 proxy_request_logs 单表（逐请求行天然带时间戳，含 data_source='session_log'
    的会话同步行），全部是带 WHERE 下界的 GROUP BY / LIMIT，毫秒级返回。
    口径与 ccs_daily 一致：input 按语义折算纯口径，因此每个聚合都必须带
    input_token_semantics 分组，桶在 Python 侧合并；
    total = 纯input + output + cache_read + cache_creation。
    """
    now = int(time.time())
    _claude_native_bg_start()
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "now": now}
    if not os.path.exists(CCS_DB):
        out["available"] = False
        out["reason"] = "未找到 CCS 数据库"
        return out
    out["available"] = True
    con = _ro(CCS_DB)
    cur = con.cursor()
    try:
        # ---- 分钟桶（最近 60 分钟，全 Agent 口径）----
        # 实现抽到 _rate_series()（v-t 图时间筛选共用同一实现）：CCS 侧剔除
        # claude/claude-desktop 会话同步行与 codex/pi 行（防与 rollout/jsonl
        # 本机直读双算），Hermes/ZCode/claude-native/其余本机 agent 逐事件并入。
        _rs = _rate_series(now, 60, 60) or {"minutes": [], "minutes_by_model": {}, "models": []}
        out["minutes"] = _rs["minutes"]
        out["minutes_by_model"] = _rs["minutes_by_model"]
        out["models_60m"] = _rs["models"]
        rate10_tok = sum(m["tokens"] for m in out["minutes"][-10:])
        rate10_out = sum(m.get("output_tokens", 0) for m in out["minutes"][-10:])
        out["rate10"] = {
            "tokens": rate10_tok,
            "output_tokens": rate10_out,
            "per_minute": rate10_tok // 10,
            "per_second": round(rate10_tok / 600.0, 1),
            "output_per_minute": rate10_out // 10,
            "output_per_second": round(rate10_out / 600.0, 1),
            "requests": sum(m["requests"] for m in out["minutes"][-10:])}
        out["rate60"] = {"tokens": sum(m["tokens"] for m in out["minutes"]),
                         "output_tokens": sum(m.get("output_tokens", 0) for m in out["minutes"]),
                         "requests": sum(m["requests"] for m in out["minutes"])}

        # ---- 小时桶（本地昨天 0 点起 48 小时：今天堆叠图 + 前一天虚线）----
        lt = time.localtime(now)
        day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                                -1, -1, -1)))
        h0 = day0 - 86400
        hours = {}
        for r in cur.execute(
                """SELECT (created_at - ?)/3600, COUNT(*),
                          SUM(input_tokens), SUM(output_tokens),
                          SUM(cache_read_tokens), SUM(cache_creation_tokens),
                          input_token_semantics, SUM(CAST(total_cost_usd AS REAL)),
                          SUM(CAST(input_cost_usd AS REAL)),
                          SUM(CAST(output_cost_usd AS REAL)),
                          SUM(CAST(cache_read_cost_usd AS REAL)),
                          SUM(CAST(cache_creation_cost_usd AS REAL))
                   FROM proxy_request_logs WHERE created_at >= ?
                   AND NOT (app_type = 'claude'
                        OR app_type IN ('codex', 'pi')
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   GROUP BY (created_at - ?)/3600, input_token_semantics""",
                (h0, h0, h0)):
            b = hours.setdefault(int(r[0]), {"input": 0, "output": 0,
                "cache_read": 0, "cache_creation": 0, "requests": 0, "cost": 0.0,
                "input_cost": 0.0, "output_cost": 0.0,
                "cache_read_cost": 0.0, "cache_creation_cost": 0.0})
            b["input"] += _pure_input(r[6], r[2], r[4], r[5])
            b["output"] += r[3] or 0
            b["cache_read"] += r[4] or 0
            b["cache_creation"] += r[5] or 0
            b["requests"] += r[1] or 0
            b["cost"] += r[7] or 0.0
            b["input_cost"] += r[8] or 0.0
            b["output_cost"] += r[9] or 0.0
            b["cache_read_cost"] += r[10] or 0.0
            b["cache_creation_cost"] += r[11] or 0.0
        # ---- 多源并入：Hermes 各 profile 会话 + ZCode 调用（2026-10-02，
        #      堆叠图「全 Agent 可统计」）。口径：input/output/cache_read 原样，
        #      Hermes cache_write 与 ZCode cache_creation 记入 cache_creation；
        #      成本并入 input_cost（非 CCS 源无分项计费）；Hermes 单条估值
        #      超过 COST_SANE_LIMIT 时剔除（terra $18.3M 脏数据）。
        def _hours_bump(bucket, inp, outp, cr, cc, cost, reqs=1):
            bucket["input"] += inp
            bucket["output"] += outp
            bucket["cache_read"] += cr
            bucket["cache_creation"] += cc
            bucket["cost"] += cost
            bucket["input_cost"] += cost
            bucket["requests"] += reqs

        for hname in _active_agents():
            hdb = _agent_db(hname)
            if not os.path.exists(hdb):
                continue
            try:
                hcon = _ro(hdb)
                # 2026-10-13：同分钟桶口径——按活跃区间分摊到小时桶
                # （请求数仍记在会话启动所在小时；跨窗只取窗内部分）
                for hr in hcon.execute(
                        """SELECT started_at, last_activity_at, ended_at,
                                  input_tokens, output_tokens,
                                  cache_read_tokens, cache_write_tokens,
                                  estimated_cost_usd
                           FROM sessions
                           WHERE started_at <= ?
                             AND COALESCE(last_activity_at, ended_at, started_at) >= ?""",
                        (h0 + 48 * 3600, h0)):
                    t0 = hr[0] or 0
                    t1 = min(hr[1] or hr[2] or hr[0] or 0, now)
                    cost = hr[7] or 0.0
                    if cost > COST_SANE_LIMIT:
                        cost = 0.0
                    hb0 = int((t0 - h0) // 3600)
                    for hb, frac in _session_spread(t0, t1, 3600, h0, 48):
                        _hours_bump(hours.setdefault(hb, {
                            "input": 0, "output": 0, "cache_read": 0,
                            "cache_creation": 0, "requests": 0, "cost": 0.0,
                            "input_cost": 0.0, "output_cost": 0.0,
                            "cache_read_cost": 0.0, "cache_creation_cost": 0.0}),
                            (hr[3] or 0) * frac, (hr[4] or 0) * frac,
                            (hr[5] or 0) * frac, (hr[6] or 0) * frac, cost * frac,
                            reqs=(1 if hb == hb0 else 0))
                hcon.close()
            except Exception:
                pass
        try:
            zdb = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
            if os.path.exists(zdb):
                zcon = _ro(zdb)
                for zr in zcon.execute(
                        """SELECT started_at, input_tokens, output_tokens,
                                  cache_read_input_tokens,
                                  cache_creation_input_tokens
                           FROM model_usage WHERE started_at >= ?""",
                        (h0 * 1000,)):
                    hb = int((zr[0] or 0) // 1000 - h0) // 3600
                    if hb < 0 or hb > 47:
                        continue
                    _hours_bump(hours.setdefault(hb, {
                        "input": 0, "output": 0, "cache_read": 0,
                        "cache_creation": 0, "requests": 0, "cost": 0.0,
                        "input_cost": 0.0, "output_cost": 0.0,
                        "cache_read_cost": 0.0, "cache_creation_cost": 0.0}),
                        zr[1] or 0, zr[2] or 0, zr[3] or 0, zr[4] or 0, 0.0)
                zcon.close()
        except Exception:
            pass
        # Claude 本机直读并入（权威逐条；CCS claude 同步行已剔除）
        try:
            for nr in _claude_native_rows(now - 14 * 86400):
                hb = int((nr["ts"] - h0) // 3600)
                if hb < 0 or hb > 47:
                    continue
                _hours_bump(hours.setdefault(hb, {
                    "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0, "requests": 0, "cost": 0.0,
                    "input_cost": 0.0, "output_cost": 0.0,
                    "cache_read_cost": 0.0, "cache_creation_cost": 0.0}),
                    nr["input"], nr["output"], nr["cache_read"],
                    nr["cache_creation"], 0.0)
        except Exception:
            pass
        # DSH 本机会话并入小时桶
        try:
            for dr in _dsh_rows(now - 14 * 86400) or []:
                hb = int((dr[0] - h0) // 3600)
                if hb < 0 or hb > 47:
                    continue
                _hours_bump(hours.setdefault(hb, {
                    "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0, "requests": 0, "cost": 0.0,
                    "input_cost": 0.0, "output_cost": 0.0,
                    "cache_read_cost": 0.0, "cache_creation_cost": 0.0}),
                    dr[2], dr[3], dr[4], dr[5], 0.0)
        except Exception:
            pass
        # 其余本机 agent 逐事件并入（与分钟桶同源，防双算见 CCS 剔除注释）
        try:
            for ev in _agent_recent_events(h0):
                hb = int(ev[0] - h0) // 3600
                if hb < 0 or hb > 47:
                    continue
                _hours_bump(hours.setdefault(hb, {
                    "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0, "requests": 0, "cost": 0.0,
                    "input_cost": 0.0, "output_cost": 0.0,
                    "cache_read_cost": 0.0, "cache_creation_cost": 0.0}),
                    ev[1], ev[2], ev[3], ev[4], 0.0)
        except Exception:
            pass
        # Antigravity 本机逐条并入小时桶
        try:
            for ar in _antigravity_native_rows(h0):
                hb = int((ar["ts"] - h0) // 3600)
                if hb < 0 or hb > 47:
                    continue
                _hours_bump(hours.setdefault(hb, {
                    "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0, "requests": 0, "cost": 0.0,
                    "input_cost": 0.0, "output_cost": 0.0,
                    "cache_read_cost": 0.0, "cache_creation_cost": 0.0}),
                    ar["input"], ar["output"], ar["cache_read"],
                    ar["cache_creation"], 0.0)
        except Exception:
            pass
        zero = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                "requests": 0, "cost": 0.0,
                "input_cost": 0.0, "output_cost": 0.0,
                "cache_read_cost": 0.0, "cache_creation_cost": 0.0}
        out["hours_yesterday"] = [hours.get(i, zero) for i in range(24)]
        out["hours_today"] = [hours.get(24 + i, zero) for i in range(24)]
        out["day0"] = day0

        # ---- 逐请求行（请求波 30 分钟 / 请求流 40 条）----
        reqs = []
        for r in cur.execute(
                """SELECT created_at, app_type, model, input_tokens, output_tokens,
                          cache_read_tokens, cache_creation_tokens,
                          input_token_semantics, CAST(total_cost_usd AS REAL),
                          status_code, latency_ms
                   FROM proxy_request_logs WHERE created_at >= ?
                   AND NOT (app_type = 'claude'
                        OR app_type IN ('codex', 'pi')
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   ORDER BY created_at DESC LIMIT 120""", (now - 1800,)):
            reqs.append({
                "ts": r[0], "app": r[1] or "", "model": r[2] or "",
                "input": _pure_input(r[7], r[3], r[5], r[6]),
                "output": r[4] or 0, "cache_read": r[5] or 0,
                "cache_creation": r[6] or 0,
                "cost": round(r[8] or 0.0, 6),
                "status": r[9], "latency_ms": r[10] or 0})
        # 多源事件并入：Hermes 各 profile 的会话（一波=一次会话启动，粒度会话级）
        # 与 ZCode model_usage 逐调用。各源保底条数（不限 30 分钟窗），否则
        # CCS 流量大时其他源永远进不了请求流。前端按 app 键聚焦过滤。
        for hname in _active_agents():
            hdb = _agent_db(hname)
            if not os.path.exists(hdb):
                continue
            try:
                hcon = _ro(hdb)
                sess_cols = [r[1] for r in hcon.execute("PRAGMA table_info(sessions)").fetchall()]
                act_expr = ("MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0))"
                            if "last_activity_at" in sess_cols else "COALESCE(ended_at, started_at, 0)")
                call_col = "api_call_count" if "api_call_count" in sess_cols else "0"
                for hr in hcon.execute(
                        f"""SELECT {act_expr} as act_ts, model, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens,
                                   estimated_cost_usd, {call_col}
                            FROM sessions WHERE {act_expr} >= ?
                            ORDER BY act_ts DESC LIMIT 6""", (now - 30 * 86400,)):
                    reqs.append({
                        "ts": hr[0] or 0, "app": f"hermes:{hname}",
                        "model": hr[1] or "", "input": hr[2] or 0,
                        "output": hr[3] or 0, "cache_read": hr[4] or 0,
                        "cache_creation": hr[5] or 0,
                        "cost": round(hr[6] or 0.0, 6),
                        "status": 200, "latency_ms": 0, "kind": "sess"})
                hcon.close()
            except Exception:
                pass
        try:
            zdb = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
            if os.path.exists(zdb):
                zcon = _ro(zdb)
                for zr in zcon.execute(
                        """SELECT started_at/1000, model_id,
                                  input_tokens, output_tokens,
                                  cache_read_input_tokens,
                                  cache_creation_input_tokens,
                                  computed_total_tokens, status, duration_ms
                           FROM model_usage WHERE started_at >= ?
                           ORDER BY started_at DESC LIMIT 25""", (now - 30 * 86400,)):
                    reqs.append({
                        "ts": int(zr[0] or 0), "app": "zcode",
                        "model": zr[1] or "",
                        "input": zr[2] or 0, "output": zr[3] or 0,
                        "cache_read": zr[4] or 0, "cache_creation": zr[5] or 0,
                        "cost": 0.0,
                        "status": 200 if (zr[7] or "") == "completed" else 500,
                        "latency_ms": zr[8] or 0, "kind": "call"})
                zcon.close()
        except Exception:
            pass
        reqs.sort(key=lambda x: x["ts"], reverse=True)
        # Claude 本机逐条并入事件流（毫秒时间戳 → 请求波）
        try:
            for nr in _claude_native_rows(now - 14 * 86400):
                if nr["ts"] < now - 1800:
                    continue
                reqs.append({
                    "ts": nr["ts"],
                    "app": ("claude-desktop"
                            if nr["entrypoint"] == "claude-desktop" else "claude"),
                    "model": nr["model"],
                    "input": nr["input"], "output": nr["output"],
                    "cache_read": nr["cache_read"],
                    "cache_creation": nr["cache_creation"],
                    "cost": 0.0, "status": 200, "latency_ms": 0})
        except Exception:
            pass
        # DSH 本机逐条并入事件流（30 分钟入请求波，保底 30 条入请求流）
        try:
            devents = [e for e in _dsh_rows(now - 14 * 86400) or []]
            devents.sort(key=lambda x: x[0], reverse=True)
            dsh_limit = max(30, sum(1 for e in devents if e[0] >= now - 1800))
            for dr in devents[:dsh_limit]:
                reqs.append({
                    "ts": dr[0], "app": "dsh", "model": dr[1] or "deepseek",
                    "input": dr[2] or 0, "output": dr[3] or 0,
                    "cache_read": dr[4] or 0, "cache_creation": dr[5] or 0,
                    "cost": 0.0, "status": 200,
                    "latency_ms": dr[7] if len(dr) > 7 else 0, "kind": "call"})
        except Exception:
            pass
        # Antigravity 本机逐条并入事件流
        try:
            for ar in _antigravity_native_rows(now - 14 * 86400):
                if ar["ts"] < now - 1800:
                    continue
                reqs.append({
                    "ts": ar["ts"], "app": "antigravity", "model": ar["model"],
                    "input": ar["input"], "output": ar["output"],
                    "cache_read": ar["cache_read"], "cache_creation": ar["cache_creation"],
                    "cost": 0.0, "status": ar["status"], "latency_ms": 0})
        except Exception:
            pass
        # Codex 本机逐条并入事件流（rollout 逐事件 → 请求波 30 分钟 / 请求流 40 条）
        try:
            cevents = [e for e in _codex_event_rows(now - 14 * 86400) if e[0] >= now - 14 * 86400]
            cevents.sort(key=lambda x: x[0], reverse=True)
            codex_limit = max(30, sum(1 for e in cevents if e[0] >= now - 1800))
            for cev in cevents[:codex_limit]:
                cts = cev[0]
                cmdl = cev[5] if len(cev) > 5 else "codex"
                reqs.append({
                    "ts": cts, "app": "codex", "model": cmdl or "codex",
                    "input": cev[1] or 0, "output": cev[2] or 0,
                    "cache_read": cev[3] or 0, "cache_creation": cev[4] or 0,
                    "cost": 0.0, "status": 200, "latency_ms": 0, "kind": "call"})
        except Exception:
            pass
        # Pi 本机逐条并入事件流
        try:
            pevents = [e for e in _pi_event_rows(now - 14 * 86400) if e[0] >= now - 14 * 86400]
            pevents.sort(key=lambda x: x[0], reverse=True)
            pi_limit = max(30, sum(1 for e in pevents if e[0] >= now - 1800))
            for pev in pevents[:pi_limit]:
                pts = pev[0]
                pmdl = pev[5] if len(pev) > 5 else "pi"
                pcost = pev[6] if len(pev) > 6 else 0.0
                reqs.append({
                    "ts": pts, "app": "pi", "model": pmdl or "pi",
                    "input": pev[1] or 0, "output": pev[2] or 0,
                    "cache_read": pev[3] or 0, "cache_creation": pev[4] or 0,
                    "cost": pcost, "status": 200, "latency_ms": 0, "kind": "call"})
        except Exception:
            pass
        reqs.sort(key=lambda x: x["ts"], reverse=True)
        out["ripple"] = [x for x in reqs if x["ts"] >= now - 1800]
        # recent 40 按「每源轮转」取：CCS 流量大时其他源的代表行不会被挤出
        by_app = {}
        for x in reqs:
            by_app.setdefault(x["app"], []).append(x)
        recent, i = [], 0
        while len(recent) < 40 and i < 60:
            for lst in by_app.values():
                if i < len(lst) and len(recent) < 40:
                    recent.append(lst[i])
            i += 1
        recent.sort(key=lambda x: x["ts"], reverse=True)
        out["recent"] = recent

        # ---- 5 小时滚动窗口（请求侧口径；配额重置点由路由层并入）----
        w = {"tokens": 0, "cost": 0.0, "requests": 0}
        for r in cur.execute(
                """SELECT COUNT(*),
                          SUM(input_tokens), SUM(output_tokens),
                          SUM(cache_read_tokens), SUM(cache_creation_tokens),
                          input_token_semantics, SUM(CAST(total_cost_usd AS REAL))
                   FROM proxy_request_logs WHERE created_at >= ?
                   AND NOT (app_type = 'claude'
                        OR app_type IN ('codex', 'pi')
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   GROUP BY input_token_semantics""", (now - 18000,)):
            w["tokens"] += (_pure_input(r[5], r[1], r[3], r[4])
                            + (r[2] or 0) + (r[3] or 0) + (r[4] or 0))
            w["cost"] += r[6] or 0.0
            w["requests"] += r[0] or 0
        try:
            for nr in _claude_native_rows(now - 14 * 86400):
                if nr["ts"] < now - 18000:
                    continue
                w["tokens"] += (nr["input"] + nr["output"] + nr["cache_read"]
                                + nr["cache_creation"])
                w["requests"] += 1
        except Exception:
            pass
        try:
            for dr in _dsh_rows(now - 14 * 86400) or []:
                if dr[0] < now - 18000:
                    continue
                w["tokens"] += dr[2] + dr[3] + dr[4] + dr[5]
                w["requests"] += 1
        except Exception:
            pass
        try:
            for ev in _agent_recent_events(now - 18000):
                w["tokens"] += ev[1] + ev[2] + ev[3] + ev[4]
                w["requests"] += 1
        except Exception:
            pass
        # Antigravity 本机逐条并入 5 小时窗口
        try:
            for ar in _antigravity_native_rows(now - 18000):
                w["tokens"] += ar["input"] + ar["output"] + ar["cache_read"] + ar["cache_creation"]
                w["requests"] += 1
        except Exception:
            pass
        # Hermes / ZCode 也并入（与分钟/小时桶同口径；会话/调用计 1 次请求）
        for hname in _active_agents():
            hdb = _agent_db(hname)
            if not os.path.exists(hdb):
                continue
            try:
                hcon = _ro(hdb)
                sess_cols = [r[1] for r in hcon.execute("PRAGMA table_info(sessions)").fetchall()]
                act_expr = ("MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0))"
                            if "last_activity_at" in sess_cols else "COALESCE(ended_at, started_at, 0)")
                for hr in hcon.execute(
                        f"""SELECT input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens
                            FROM sessions WHERE {act_expr} >= ?""", (now - 18000,)):
                    w["tokens"] += ((hr[0] or 0) + (hr[1] or 0) + (hr[2] or 0)
                                    + (hr[3] or 0))
                    w["requests"] += 1
                hcon.close()
            except Exception:
                pass
        try:
            zdb = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
            if os.path.exists(zdb):
                zcon = _ro(zdb)
                for zr in zcon.execute(
                        """SELECT input_tokens, output_tokens,
                                  cache_read_input_tokens,
                                  cache_creation_input_tokens
                           FROM model_usage WHERE started_at >= ?""",
                        ((now - 18000) * 1000,)):
                    w["tokens"] += ((zr[0] or 0) + (zr[1] or 0) + (zr[2] or 0)
                                    + (zr[3] or 0))
                    w["requests"] += 1
                zcon.close()
        except Exception:
            pass
        w["cost"] = round(w["cost"], 4)
        out["window5h"] = w

        # ---- 工具卡：14 天日序列（proxy + 会话同步 rollups 两路相加，与 ccs_by_tool
        #      同约定）+ 最后活动时间 ----
        d14 = time.strftime("%Y-%m-%d", time.localtime(now - 13 * 86400))
        series = {}
        for r in cur.execute(
                """SELECT date(created_at,'unixepoch','localtime'), app_type, COUNT(*),
                          SUM(input_tokens), SUM(output_tokens),
                          SUM(cache_read_tokens), SUM(cache_creation_tokens),
                          input_token_semantics
                   FROM proxy_request_logs WHERE created_at >= ?
                   AND NOT (app_type = 'claude'
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   GROUP BY 1, 2, 8""", (now - 14 * 86400,)):
            e = series.setdefault((r[0], r[1]), {"tokens": 0, "requests": 0, "cost": 0.0,
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})
            inp = _pure_input(r[7], r[3], r[5], r[6])
            e["input"] += inp
            e["output"] += r[4] or 0
            e["cache_read"] += r[5] or 0
            e["cache_creation"] += r[6] or 0
            e["tokens"] += inp + (r[4] or 0) + (r[5] or 0) + (r[6] or 0)
            e["requests"] += r[2] or 0
        for r in cur.execute(
                """SELECT date, app_type, SUM(request_count), SUM(input_tokens),
                          SUM(output_tokens), SUM(cache_read_tokens),
                          SUM(cache_creation_tokens), SUM(CAST(total_cost_usd AS REAL))
                   FROM usage_daily_rollups WHERE date >= ? GROUP BY 1, 2""", (d14,)):
            e = series.setdefault((r[0], r[1]), {"tokens": 0, "requests": 0, "cost": 0.0,
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})
            e["input"] += r[3] or 0
            e["output"] += r[4] or 0
            e["cache_read"] += r[5] or 0
            e["cache_creation"] += r[6] or 0
            e["tokens"] += (r[3] or 0) + (r[4] or 0) + (r[5] or 0) + (r[6] or 0)
            e["requests"] += r[2] or 0
            e["cost"] += r[7] or 0.0
        # Claude 本机直读并入日序列
        try:
            import datetime as _dt
            for nr in _claude_native_rows(now - 14 * 86400):
                d = time.strftime("%Y-%m-%d", time.localtime(nr["ts"]))
                key = ("claude-desktop" if nr["entrypoint"] == "claude-desktop"
                       else "claude")
                e = series.setdefault((d, key), {"tokens": 0, "requests": 0,
                    "cost": 0.0, "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0})
                e["input"] += nr["input"]
                e["output"] += nr["output"]
                e["cache_read"] += nr["cache_read"]
                e["cache_creation"] += nr["cache_creation"]
                e["tokens"] += (nr["input"] + nr["output"] + nr["cache_read"]
                                + nr["cache_creation"])
                e["requests"] += 1
        except Exception:
            pass
        # DSH 本机会话并入日序列
        try:
            for dr in _dsh_rows(now - 14 * 86400) or []:
                d = time.strftime("%Y-%m-%d", time.localtime(dr[0]))
                e = series.setdefault((d, "dsh"), {"tokens": 0, "requests": 0,
                    "cost": 0.0, "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0})
                e["input"] += dr[2]
                e["output"] += dr[3]
                e["cache_read"] += dr[4]
                e["cache_creation"] += dr[5]
                e["tokens"] += dr[2] + dr[3] + dr[4] + dr[5]
                e["requests"] += 1
        except Exception:
            pass
        # Antigravity 本机逐条并入日序列
        try:
            for ar in _antigravity_native_rows(now - 14 * 86400):
                d = time.strftime("%Y-%m-%d", time.localtime(ar["ts"]))
                e = series.setdefault((d, "antigravity"), {"tokens": 0, "requests": 0,
                    "cost": 0.0, "input": 0, "output": 0, "cache_read": 0,
                    "cache_creation": 0})
                e["input"] += ar["input"]
                e["output"] += ar["output"]
                e["cache_read"] += ar["cache_read"]
                e["cache_creation"] += ar["cache_creation"]
                e["tokens"] += (ar["input"] + ar["output"] + ar["cache_read"]
                                + ar["cache_creation"])
                e["requests"] += 1
        except Exception:
            pass
        dates = [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
                 for i in range(13, -1, -1)]
        by_tool = {}
        for (d, app), e in series.items():
            by_tool.setdefault(app, {})[d] = e
        out["tools_series"] = {app: [by_tool.get(app, {}).get(d) or
                                     {"tokens": 0, "requests": 0, "cost": 0.0,
                                      "input": 0, "output": 0, "cache_read": 0,
                                      "cache_creation": 0}
                                     for d in dates] for app in by_tool}
        out["tools_dates"] = dates
        # ---- 最后活动时间：按「聚焦键」给全源口径，与 hours_today_by_app /
        #      事件流同一批数据。只查 proxy_request_logs 会让 Claude 桌面端显示
        #      CCS 代理聊天行的旧时间（实测显示「10 天前」而当天有请求），
        #      Codex/ZCode/Hermes 各卡干脆没有值（前端只能显示 —）。----
        last = {}

        def _bump_last(key, ts):
            if not key or not ts:
                return
            ts = int(ts)
            if ts > last.get(key, 0):
                last[key] = ts

        def _bump_mtime_last(key, paths):
            best = 0
            for p in paths:
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt > best:
                    best = mt
            _bump_last(key, best)

        last_window = now - 30 * 86400
        for r in cur.execute(
                """SELECT app_type, MAX(created_at) FROM proxy_request_logs
                   WHERE created_at >= ?
                   AND NOT (app_type = 'claude' OR app_type IN ('codex', 'pi')
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   GROUP BY app_type""", (last_window,)):
            _bump_last(r[0], r[1])
        for hname in _active_agents():
            hdb = _agent_db(hname)
            if not os.path.exists(hdb):
                continue
            try:
                hcon = _ro(hdb)
                sess_cols = [r[1] for r in hcon.execute("PRAGMA table_info(sessions)").fetchall()]
                act_expr = ("MAX(COALESCE(last_activity_at, 0), COALESCE(ended_at, 0), COALESCE(started_at, 0))"
                            if "last_activity_at" in sess_cols else "COALESCE(ended_at, started_at, 0)")
                row = hcon.execute(
                    f"SELECT MAX({act_expr}) FROM sessions WHERE {act_expr} >= ?",
                    (last_window,)).fetchone()
                hcon.close()
                if row and row[0]:
                    _bump_last(f"hermes:{hname}", row[0])
            except Exception:
                pass
        try:
            zdb = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
            if os.path.exists(zdb):
                zcon = _ro(zdb)
                row = zcon.execute(
                    "SELECT MAX(started_at) FROM model_usage WHERE started_at >= ?",
                    (last_window * 1000,)).fetchone()
                zcon.close()
                if row and row[0]:
                    _bump_last("zcode", int(row[0]) // 1000)
        except Exception:
            pass
        try:
            for nr in _claude_native_rows(now - 14 * 86400):
                _bump_last("claude-desktop" if nr["entrypoint"] == "claude-desktop"
                           else "claude", nr["ts"])
        except Exception:
            pass
        try:
            for dr in _dsh_rows(last_window) or []:
                _bump_last("dsh", dr[0])
        except Exception:
            pass
        try:
            for ar in _antigravity_native_rows(last_window):
                _bump_last("antigravity", ar["ts"])
        except Exception:
            pass
        # Codex / Pi 的最后活动取会话文件 mtime：等价于该会话最后一次写入，
        # 且不用解析内容（30 天事件窗口冷启动要多读 4s，这里只要 30ms）。
        try:
            _bump_mtime_last("codex", _codex_paths())
        except Exception:
            pass
        try:
            _bump_mtime_last("pi", _pi_session_paths())
        except Exception:
            pass
        out["tools_last"] = last

        # ---- 今天按小时按 Agent（焦点模式堆叠图；CCS 之外 Hermes/ZCode
        #      同口径并入，键与事件流一致：hermes:<profile> / zcode）----
        hours_app = {}
        for r in cur.execute(
                """SELECT (created_at - ?)/3600, app_type,
                          SUM(input_tokens), SUM(output_tokens),
                          SUM(cache_read_tokens), SUM(cache_creation_tokens),
                          input_token_semantics,
                          SUM(CAST(total_cost_usd AS REAL)),
                          SUM(CAST(input_cost_usd AS REAL)),
                          SUM(CAST(output_cost_usd AS REAL)),
                          SUM(CAST(cache_read_cost_usd AS REAL)),
                          SUM(CAST(cache_creation_cost_usd AS REAL))
                   FROM proxy_request_logs
                   WHERE created_at >= ? AND created_at < ?
                   AND NOT (app_type = 'claude'
                        OR (app_type = 'claude-desktop'
                            AND data_source = 'session_log'))
                   GROUP BY (created_at - ?)/3600, app_type, input_token_semantics""",
                (day0, day0, day0 + 86400, day0)):
            h = int(r[0])
            if h < 0 or h > 23:
                continue
            app = r[1] or ""
            per = hours_app.setdefault(app, [dict(zero) for _ in range(24)])
            b = per[h]
            b["input"] += _pure_input(r[6], r[2], r[4], r[5])
            b["output"] += r[3] or 0
            b["cache_read"] += r[4] or 0
            b["cache_creation"] += r[5] or 0
            b["cost"] = b.get("cost", 0.0) + (r[7] or 0.0)
            b["input_cost"] = b.get("input_cost", 0.0) + (r[8] or 0.0)
            b["output_cost"] = b.get("output_cost", 0.0) + (r[9] or 0.0)
            b["cache_read_cost"] = b.get("cache_read_cost", 0.0) + (r[10] or 0.0)
            b["cache_creation_cost"] = b.get("cache_creation_cost", 0.0) + (r[11] or 0.0)
        # Hermes / ZCode 并入 by_app（与事件流聚焦键一致）
        for hname in _active_agents():
            hdb = _agent_db(hname)
            if not os.path.exists(hdb):
                continue
            try:
                hcon = _ro(hdb)
                # 2026-10-13：按活跃区间分摊到今日小时桶（跨天会话今日部分
                # 不再丢失）；请求数仍记在会话启动所在小时
                for hr in hcon.execute(
                        """SELECT started_at, last_activity_at, ended_at,
                                  input_tokens, output_tokens,
                                  cache_read_tokens, cache_write_tokens,
                                  estimated_cost_usd
                           FROM sessions
                           WHERE started_at < ?
                             AND COALESCE(last_activity_at, ended_at, started_at) >= ?""",
                        (day0 + 86400, day0)):
                    t0 = hr[0] or 0
                    t1 = min(hr[1] or hr[2] or hr[0] or 0, now)
                    cost = hr[7] or 0.0
                    if cost > COST_SANE_LIMIT:
                        cost = 0.0
                    spread = _session_spread(t0, t1, 3600, day0, 24)
                    if not spread:
                        continue
                    per = hours_app.setdefault(
                        f"hermes:{hname}", [dict(zero) for _ in range(24)])
                    hb0 = int((t0 - day0) // 3600)
                    for hb, frac in spread:
                        _hours_bump(per[hb], (hr[3] or 0) * frac, (hr[4] or 0) * frac,
                                    (hr[5] or 0) * frac, (hr[6] or 0) * frac, cost * frac,
                                    reqs=(1 if hb == hb0 else 0))
                hcon.close()
            except Exception:
                pass
        try:
            zdb = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
            if os.path.exists(zdb):
                zcon = _ro(zdb)
                for zr in zcon.execute(
                        """SELECT started_at, input_tokens, output_tokens,
                                  cache_read_input_tokens,
                                  cache_creation_input_tokens
                           FROM model_usage
                           WHERE started_at >= ? AND started_at < ?""",
                        (day0 * 1000, (day0 + 86400) * 1000)):
                    hb = int((zr[0] or 0) // 1000 - day0) // 3600
                    if hb < 0 or hb > 23:
                        continue
                    per = hours_app.setdefault(
                        "zcode", [dict(zero) for _ in range(24)])
                    _hours_bump(per[hb], zr[1] or 0, zr[2] or 0,
                                zr[3] or 0, zr[4] or 0, 0.0)
                zcon.close()
        except Exception:
            pass
        # Claude 本机直读并入 by_app（entrypoint → 聚焦键）
        try:
            for nr in _claude_native_rows(now - 14 * 86400):
                hb = int((nr["ts"] - day0) // 3600)
                if hb < 0 or hb > 23:
                    continue
                key = ("claude-desktop" if nr["entrypoint"] == "claude-desktop"
                       else "claude")
                per = hours_app.setdefault(key, [dict(zero) for _ in range(24)])
                _hours_bump(per[hb], nr["input"], nr["output"],
                            nr["cache_read"], nr["cache_creation"], 0.0)
        except Exception:
            pass
        # DSH 本机会话并入 by_app（聚焦键 dsh）
        try:
            for dr in _dsh_rows(now - 14 * 86400) or []:
                hb = int((dr[0] - day0) // 3600)
                if hb < 0 or hb > 23:
                    continue
                per = hours_app.setdefault("dsh", [dict(zero) for _ in range(24)])
                _hours_bump(per[hb], dr[2], dr[3], dr[4], dr[5], 0.0)
        except Exception:
            pass
        # Antigravity 本机逐条并入 by_app（聚焦键 antigravity）
        try:
            for ar in _antigravity_native_rows(now - 14 * 86400):
                hb = int((ar["ts"] - day0) // 3600)
                if hb < 0 or hb > 23:
                    continue
                per = hours_app.setdefault("antigravity", [dict(zero) for _ in range(24)])
                _hours_bump(per[hb], ar["input"], ar["output"],
                            ar["cache_read"], ar["cache_creation"], 0.0)
        except Exception:
            pass
        # Codex 本机逐条并入 by_app（聚焦键 codex）
        try:
            for cev in _codex_event_rows(day0):
                cts = cev[0]
                if cts < day0 or cts >= day0 + 86400:
                    continue
                hb = int(cts - day0) // 3600
                if hb < 0 or hb > 23:
                    continue
                per = hours_app.setdefault("codex", [dict(zero) for _ in range(24)])
                _hours_bump(per[hb], cev[1] or 0, cev[2] or 0,
                            cev[3] or 0, cev[4] or 0, 0.0)
        except Exception:
            pass
        # Pi 本机逐条并入 by_app（聚焦键 pi）
        try:
            for pev in _pi_event_rows(day0):
                pts = pev[0]
                if pts < day0 or pts >= day0 + 86400:
                    continue
                hb = int(pts - day0) // 3600
                if hb < 0 or hb > 23:
                    continue
                pcost = pev[6] if len(pev) > 6 else 0.0
                per = hours_app.setdefault("pi", [dict(zero) for _ in range(24)])
                _hours_bump(per[hb], pev[1] or 0, pev[2] or 0,
                            pev[3] or 0, pev[4] or 0, pcost)
        except Exception:
            pass
        out["hours_today_by_app"] = hours_app

        # ---- 缓存节省：最近 30 天 cache_read 按官方价差折算 ----
        saved = 0.0
        try:
            price = {}
            for p in cur.execute(
                    """SELECT model_id, input_cost_per_million,
                              cache_read_cost_per_million FROM model_pricing"""):
                try:
                    price[p[0]] = (float(p[1] or 0), float(p[2] or 0))
                except (TypeError, ValueError):
                    pass
            for r in cur.execute(
                    """SELECT COALESCE(pricing_model, model), SUM(cache_read_tokens)
                       FROM proxy_request_logs WHERE created_at >= ?
                       GROUP BY 1""", (now - 30 * 86400,)):
                pp = price.get(r[0])
                if pp and r[1]:
                    saved += r[1] / 1e6 * max(pp[0] - pp[1], 0.0)
            saved = round(saved, 2)
        except Exception:
            saved = None
        out["cache_saved_usd_30d"] = saved
    except Exception as e:
        out["available"] = False
        out["reason"] = str(e)
    finally:
        con.close()
    return out


if __name__ == "__main__":
    try:
        sys_enc = "utf-8"
        print("=" * 60)
        print("SUMMARY (all time)")
        print(json.dumps(summary(), ensure_ascii=False, indent=2)[:1500])
        print("\n" + "=" * 60)
        print("AGENTS")
        print(json.dumps(hermes_agents(), ensure_ascii=False, indent=2)[:2000])
        print("\n" + "=" * 60)
        print("CCS MODELS (top 8)")
        print(json.dumps(ccs_models(limit=8), ensure_ascii=False, indent=2)[:1200])
        print("\n" + "=" * 60)
        print("CCS DAILY")
        print(json.dumps(ccs_daily(), ensure_ascii=False, indent=2)[:1500])
    except Exception as e:
        import traceback
        traceback.print_exc()
