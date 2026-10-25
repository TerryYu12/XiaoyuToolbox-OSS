# -*- coding: utf-8 -*-
"""Epic 每周免费游戏：检测 + 半自动领取（游戏专区）。

检测走 Epic 公开 GraphQL（无需登录）：
  https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions
discountPercentage == 0 且处于时间窗内 = 当前免费；discountSetting 同口径给下周预告。

领取为半自动：本模块不做登录与下单协议（Epic 风控强，纯 API 路线维护成本高），
而是由 tray_app 注册的领取窗口回调打开真实 WebView2 详情页，注入 JS 探测
「立即领取 / 在库中 / 登录」等状态并逐个点击；登录或验证码由用户在窗口内完成，
登录态随持久化 profile 保留，之后每周自动领取无需再登录。

依赖方向（防循环导入）：
  stats_server → epic_claimer（/api/games/*）
  tray_app → epic_claimer（注册领取窗口回调；JS 探测脚本常量）
数据：epic_claimer_cache.json（exe 同目录，检测缓存 + 领取记录 + 尝试限次）。
"""
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

GRAPHQL_URL = ('https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions'
               '?locale=zh-CN&country=CN&allowCountries=CN')
STORE_BASE = 'https://store.epicgames.com/zh-CN/p/'
FREE_GAMES_PAGE = 'https://store.epicgames.com/zh-CN/free-games'

CHECK_INTERVAL = 2 * 3600          # 缓存超过 2 小时即视为过期，下次调度醒来补检
CLAIM_ATTEMPT_LIMIT = 3            # 同一游戏同一自然日最多自动尝试次数（防风控/防弹窗骚扰）
CLAIM_COOLDOWN = 10 * 60           # 领取运行结束后至少间隔 10 分钟才允许再次自动触发

_lock = threading.Lock()
_sched_stop = threading.Event()
_sched_started = False
_claim_running = False             # 领取窗口是否在跑（含手动触发）
_claim_progress = []               # [{"title", "status"}] 供前端展示
_last_auto_claim_at = 0.0

# tray_app 启动时注册（与 stats_server.set_window_show_callback 同一模式）
_claim_window_opener = None

CACHE_NAME = 'epic_claimer_cache.json'


def _cache_path():
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CACHE_NAME)


def _load_cache():
    try:
        with open(_cache_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:               # noqa: BLE001 缓存损坏不阻塞，重建
        return {}


def _save_cache(cache):
    path = _cache_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def set_claim_window_opener(cb):
    """注册领取窗口回调 cb(items, on_done) -> bool（线程安全，启动期调用）。"""
    global _claim_window_opener
    _claim_window_opener = cb


# -------------------------------------------------- 检测（公开 GraphQL，无需登录）

def _parse_element(el):
    """catalog element -> 统一字段；无有效 slug 的条目丢弃（无法打开详情页）。"""
    cat = el.get('catalogNs') or {}
    slug = None
    for m in cat.get('mappings') or []:
        if m.get('pageType') == 'productHome' and m.get('pageSlug'):
            slug = m['pageSlug']
            break
    if not slug:
        ps = el.get('productSlug') or ''
        slug = ps.split('/')[0] if ps else None
    if not slug:
        return None
    image = ''
    for img in el.get('keyImages') or []:
        if img.get('type') == 'OfferImageWide':
            image = img.get('url') or ''
            break
    return {
        'id': el.get('id') or slug,
        'title': el.get('title') or slug,
        'slug': slug,
        'image': image,
        'url': STORE_BASE + slug,
    }


def _is_free_promo(offer):
    """promotion offer 是否 100% 折扣（免费）。"""
    ds = offer.get('discountSetting') or {}
    return ds.get('discountPercentage') == 0


def _within_window(offer, now=None):
    """promotion offer 是否处于当前生效时间窗（start 已过、end 未过；缺失按未过）。"""
    now = now or datetime.now(timezone.utc)

    def _dt(key):
        raw = offer.get(key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except Exception:       # noqa: BLE001
            return None

    start, end = _dt('startDate'), _dt('endDate')
    if start and now < start:
        return False
    if end and now > end:
        return False
    return True


def fetch_free_games(timeout=15, retries=3):
    """拉取当前免费与下周预告。返回 (current, upcoming, error)；网络异常时 error 非空。"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    req = urllib.request.Request(GRAPHQL_URL, headers=headers)
    last_exc = None
    data = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            last_exc = None
            break
        except Exception as exc:    # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5)
    if last_exc is not None:
        return [], [], str(last_exc)
    current, upcoming, seen = [], [], set()
    for el in (data.get('data', {}).get('Catalog', {}).get('searchStore', {})
               .get('elements') or []):
        item = _parse_element(el)
        if not item:
            continue
        promos = el.get('promotions') or {}
        cur = promos.get('promotionalOffers') or []
        nxt = promos.get('upcomingPromotionalOffers') or []
        active = any(_is_free_promo(o) and _within_window(o)
                     for p in cur for o in p.get('promotionalOffers', []))
        soon = [o for p in nxt for o in p.get('promotionalOffers', [])
                if _is_free_promo(o)]
        if active:
            for p in cur:
                for o in p.get('promotionalOffers', []):
                    if _is_free_promo(o) and _within_window(o):
                        item['start'], item['end'] = o.get('startDate'), o.get('endDate')
                        break
            if item['id'] not in seen:
                seen.add(item['id'])
                current.append(item)
        elif soon:
            o = soon[0]
            item['start'], item['end'] = o.get('startDate'), o.get('endDate')
            if item['id'] not in seen:
                seen.add(item['id'])
                upcoming.append(item)
    upcoming.sort(key=lambda x: x.get('start') or '')
    return current, upcoming, None


def refresh_now(force=False):
    """立即检测并落盘缓存。返回 {ok, error}；60s 内有成功结果且非 force 则直接复用。"""
    with _lock:
        cache = _load_cache()
        last_ok = cache.get('lastCheckOkAt') or 0
        if not force and time.time() - last_ok < 60:
            return {'ok': True, 'cached': True}
    current, upcoming, err = fetch_free_games()
    cache = _load_cache()
    if err:
        cache['lastCheckAt'] = datetime.now().isoformat(timespec='seconds')
        cache['lastCheckError'] = err
        _save_cache(cache)
        return {'ok': False, 'error': err}
    cache['current'] = current
    cache['upcoming'] = upcoming
    cache['lastCheckAt'] = datetime.now().isoformat(timespec='seconds')
    cache['lastCheckOkAt'] = time.time()
    cache['lastCheckError'] = None
    _save_cache(cache)
    return {'ok': True, 'current': len(current), 'upcoming': len(upcoming)}


# -------------------------------------------------- 状态查询（/api/games/epic）

def get_state():
    with _lock:
        cache = _load_cache()
    claims = cache.get('claims') or {}
    now_day = datetime.now().strftime('%Y-%m-%d')
    attempts = cache.get('attempts') or {}

    def _decor(item):
        out = dict(item)
        rec = claims.get(item['id'])
        if rec:
            out['status'] = 'claimed'
            out['claimedAt'] = rec.get('claimedAt')
        elif (attempts.get(f'{now_day}|{item["id"]}') or 0) >= CLAIM_ATTEMPT_LIMIT:
            out['status'] = 'attempted'
        else:
            out['status'] = 'unclaimed'
        return out

    return {
        'ok': True,
        'current': [_decor(x) for x in (cache.get('current') or [])],
        'upcoming': cache.get('upcoming') or [],
        'lastCheck': cache.get('lastCheck'),
        'lastCheckError': cache.get('lastCheckError'),
        'auto': bool(cache.get('auto', True)),
        'claiming': {'running': _claim_running, 'progress': list(_claim_progress)},
        'history': [dict(v, offerId=k) for k, v in
                    sorted((cache.get('claims') or {}).items(),
                           key=lambda kv: kv[1].get('claimedAt') or '', reverse=True)],
    }


def set_auto(enabled):
    with _lock:
        cache = _load_cache()
        cache['auto'] = bool(enabled)
        _save_cache(cache)
    return {'ok': True, 'auto': bool(enabled)}


# -------------------------------------------------- 领取（回调 tray_app 开窗口）

def _attempt_key(offer_id):
    return f'{datetime.now().strftime("%Y-%m-%d")}|{offer_id}'


def start_claim(offer_ids=None, source='manual'):
    """触发领取窗口。offer_ids 为空 = 领取所有当前未领取的。

    返回 {ok, started, skipped, reason}；窗口回调未注册或已在领取中时 ok=False。
    """
    global _claim_running, _claim_progress, _last_auto_claim_at
    with _lock:
        cache = _load_cache()
    claims = cache.get('claims') or {}
    now_day = datetime.now().strftime('%Y-%m-%d')
    attempts = cache.get('attempts') or {}
    current = cache.get('current') or []
    if offer_ids:
        items = [x for x in current if x['id'] in set(offer_ids)]
    else:
        if source == 'auto':
            items = [x for x in current if x['id'] not in claims
                     and (attempts.get(f'{now_day}|{x["id"]}') or 0) < CLAIM_ATTEMPT_LIMIT]
        else:
            items = [x for x in current if x['id'] not in claims]
    if not items:
        if source == 'auto':
            return {'ok': False, 'started': False, 'reason': '没有可领取的游戏（已领取或今日尝试已达上限）'}
        return {'ok': False, 'started': False, 'reason': '没有可领取的游戏（所有游戏已在库中）'}
    if source == 'manual':
        # 手动明确触发：重置选中项当日尝试计数，解除此前自动失败导致的尝试锁定
        with _lock:
            c = _load_cache()
            att = c.setdefault('attempts', {})
            for x in items:
                att.pop(f'{now_day}|{x["id"]}', None)
            _save_cache(c)
    with _lock:
        if _claim_running:
            return {'ok': False, 'started': False, 'reason': '领取正在进行中'}
        if source == 'auto' and time.time() - _last_auto_claim_at < CLAIM_COOLDOWN:
            return {'ok': False, 'started': False, 'reason': '距上次自动领取不足冷却时间'}
        _claim_running = True
        _claim_progress = [{'title': x['title'], 'status': 'pending'} for x in items]
    started = False

    def _on_done(results):
        global _claim_running, _claim_progress, _last_auto_claim_at
        with _lock:
            cache = _load_cache()
            claims = cache.setdefault('claims', {})
            attempts = cache.setdefault('attempts', {})
            for item, status in zip(items, results):
                if status in ('claimed', 'owned'):
                    claims[item['id']] = {'title': item['title'],
                                          'claimedAt': datetime.now().isoformat(timespec='seconds'),
                                          'method': 'window'}
                else:
                    attempts[f'{now_day}|{item["id"]}'] = \
                        (attempts.get(f'{now_day}|{item["id"]}') or 0) + 1
            _save_cache(cache)
            _claim_running = False
            _last_auto_claim_at = time.time()

    opener = _claim_window_opener
    if opener is None:
        with _lock:
            _claim_running = False
            _claim_progress = []
        return {'ok': False, 'started': False, 'reason': '领取窗口未就绪（原生窗口不可用）'}
    try:
        started = bool(opener(items, _on_done))
    except Exception as exc:    # noqa: BLE001
        print(f'[epic] 领取窗口启动异常: {exc}', flush=True)
        started = False
    if not started:
        with _lock:
            _claim_running = False
            _claim_progress = []
        return {'ok': False, 'started': False, 'reason': '领取窗口未能启动'}
    return {'ok': True, 'started': True, 'items': [x['title'] for x in items]}


def claim_progress_update(idx, status):
    """领取窗口驱动循环回报每个条目的进行状态（供 /api/games/epic 展示）。"""
    with _lock:
        if 0 <= idx < len(_claim_progress):
            _claim_progress[idx]['status'] = status


# -------------------------------------------------- 领取窗口页面判定（tray_app 驱动循环用）

# 探测脚本：收集可见按钮/链接文本与页面关键文本，交 Python 侧判定（选择器集中、便于调优）
# 探测脚本：收集可见按钮/链接/输入框文本与页面关键文本，递归探测同源 iframe，检测安全验证
PROBE_JS = """
(function () {
  var btns = [];
  var bodies = [];
  var hasCaptcha = false;

  function inspectDoc(doc) {
    if (!doc) return;
    try {
      if (doc.querySelector('iframe[src*="hcaptcha"], iframe[src*="captcha"], #h_captcha_challenge_egs_quick_purchase_prod, div[class*="captcha"], div[id*="h_captcha"]')) {
        hasCaptcha = true;
      }
      var b = (doc.body && (doc.body.innerText || doc.body.textContent)) || '';
      if (b) bodies.push(b.slice(0, 2000));

      var els = doc.querySelectorAll('button, a[role="button"], a[data-component], input[type="button"], input[type="submit"]');
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        var t = (el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
        if (t && t.length < 80 && btns.indexOf(t) === -1) {
          btns.push(t);
        }
      }
    } catch (e) {}

    try {
      var iframes = doc.querySelectorAll('iframe');
      for (var j = 0; j < iframes.length; j++) {
        var f = iframes[j];
        if (f.src && (f.src.indexOf('hcaptcha') !== -1 || f.src.indexOf('captcha') !== -1)) {
          hasCaptcha = true;
        }
        try {
          var idoc = f.contentDocument || (f.contentWindow && f.contentWindow.document);
          if (idoc) inspectDoc(idoc);
        } catch (e) {}
      }
    } catch (e) {}
  }

  inspectDoc(document);

  return JSON.stringify({
    url: location.href,
    body: bodies.join('\\n').toLowerCase(),
    btns: btns.slice(0, 80),
    hasCaptcha: hasCaptcha
  });
})()
"""

# 点击脚本：按文本（精确优先，回退子串）找可见元素，并在按钮和内部文字节点上派发完整指针/鼠标事件
CLICK_JS_FMT = """
(function (text) {
  var target = (text || '').toLowerCase().trim();

  function dispatchClick(el) {
    try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
    var span = el.querySelector('span') || el;
    var rect = span.getBoundingClientRect();
    var evOpts = {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
      button: 0,
      buttons: 1
    };
    try { el.focus(); } catch (e) {}
    try { span.dispatchEvent(new PointerEvent('pointerdown', evOpts)); } catch (e) {}
    try { span.dispatchEvent(new MouseEvent('mousedown', evOpts)); } catch (e) {}
    try { span.dispatchEvent(new PointerEvent('pointerup', evOpts)); } catch (e) {}
    try { span.dispatchEvent(new MouseEvent('mouseup', evOpts)); } catch (e) {}
    try { span.click(); } catch (e) {}
    try { if (span !== el) el.click(); } catch (e) {}
    return 'clicked';
  }

  function searchDoc(doc) {
    if (!doc) return null;
    try {
      var els = doc.querySelectorAll('button, a[role="button"], input[type="button"], input[type="submit"]');
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        var t = (el.textContent || el.value || '').trim().replace(/\\s+/g, ' ').toLowerCase();
        if (t === target) {
          return dispatchClick(el);
        }
      }
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        var t = (el.textContent || el.value || '').trim().replace(/\\s+/g, ' ').toLowerCase();
        if (t.indexOf(target) !== -1 || target.indexOf(t) !== -1) {
          return dispatchClick(el);
        }
      }
    } catch (e) {}

    try {
      var iframes = doc.querySelectorAll('iframe');
      for (var j = 0; j < iframes.length; j++) {
        try {
          var idoc = iframes[j].contentDocument || (iframes[j].contentWindow && iframes[j].contentWindow.document);
          if (idoc) {
            var res = searchDoc(idoc);
            if (res) return res;
          }
        } catch (e) {}
      }
    } catch (e) {}
    return null;
  }

  return searchDoc(document) || 'not-found';
})("__TEXT__")
"""

# 词表按判定优先级排列；文本一律小写比对
_CHECKOUT_WORDS = ('下订单', 'place order', '提交订单', '确认订单')
_AGREE_WORDS = ('我同意', 'i agree', '同意并继续')
_AGE_WORDS = ('继续', '查看游戏', '查看产品', '我已年满', 'continue', 'view game', 'view product')
_COOKIE_WORDS = ('接受所有 cookie', '接受所有', 'accept all cookies', 'accept all')
_GET_WORDS = ('立即领取', '获取', 'claim', 'get')
_OWNED_WORDS = ('in library', 'in your library', '已在库中', '在库中', '已拥有')
_DONE_WORDS = ('thanks', '谢谢', 'thank you')
_LOGIN_WORDS = ('登录', 'sign in')
_AGE_TRIGGER_WORDS = ('成人', '年龄', '成熟受众', 'mature', 'age restricted', '18+')


def _btns_contain(btns, words, exact=False):
    for b in btns:
        lb = b.lower()
        for w in words:
            if (exact and lb == w) or (not exact and w in lb):
                return b
    return None


def judge_page(probe_json):
    """探测结果 -> (kind, click_text)。kind ∈ login/owned/done/captcha/checkout/age_gate/cookie/get/unknown。

    点击文本对 checkout/age_gate/cookie/get 返回；captcha/login/owned/done 状态无需自动点击动作。
    """
    try:
        data = json.loads(probe_json) if isinstance(probe_json, str) else (probe_json or {})
    except Exception:               # noqa: BLE001
        return 'unknown', None
    btns = [str(b) for b in (data.get('btns') or [])]
    body = (data.get('body') or '').lower()
    url = (data.get('url') or '').lower()
    has_captcha = bool(data.get('hasCaptcha'))

    if '/login' in url or '/id/login' in url:
        return 'login', None

    # 1. 已拥有优先拦截（已拥有详情页主按钮同为"获取/Get Started"，不能当可领点）
    if (_btns_contain(btns, _OWNED_WORDS)
            or any(w in body for w in _OWNED_WORDS)):
        return 'owned', None

    # 2. 谢谢页 / 下单完成
    if any(w in body for w in _DONE_WORDS):
        return 'done', None

    # 3. 验证码感知（hCaptcha/Arkose 等，唤起前台由人工处理）
    if has_captcha or 'hcaptcha' in body or 'captcha' in body:
        return 'captcha', None

    # 4. 0 元结算确认层（优先于主按钮，防止弹层打开后仍误点背景的「获取」）
    agree_btn = _btns_contain(btns, _AGREE_WORDS)
    if agree_btn:
        return 'checkout', agree_btn
    order_btn = _btns_contain(btns, _CHECKOUT_WORDS)
    if order_btn:
        return 'checkout', order_btn

    # 5. 年龄门禁 / 成人内容提示页
    age_btn = _btns_contain(btns, _AGE_WORDS, exact=True)
    if age_btn and any(w in body for w in _AGE_TRIGGER_WORDS):
        return 'age_gate', age_btn

    # 6. Cookie 弹窗拦截（避免阻挡视线与点击）
    cookie_btn = _btns_contain(btns, _COOKIE_WORDS)
    if cookie_btn:
        return 'cookie', cookie_btn

    # 7. 详情页主按钮
    get_btn = _btns_contain(btns, _GET_WORDS, exact=True)
    if get_btn:
        return 'get', get_btn

    # 8. 登录页特征（导航栏登录或首屏以登录开头）
    if _btns_contain(btns, _LOGIN_WORDS, exact=True) or body.startswith('登录'):
        return 'login', None

    return 'unknown', None


# -------------------------------------------------- 调度（仿 ai_fetcher：Event 轮询 + 幂等）

def _scheduler_loop():
    """每 10 分钟醒一次：缓存过期补检测；开启自动领取且有未领的当前免费游戏时触发窗口。"""
    while not _sched_stop.wait(600):
        try:
            state = get_state()
            stale = (not state['lastCheck']) or \
                (time.time() - (_load_cache().get('lastCheckOkAt') or 0) > CHECK_INTERVAL)
            if stale:
                refresh_now()
                state = get_state()
            if not state.get('auto'):
                continue
            pending = [x for x in state.get('current', []) if x['status'] == 'unclaimed']
            if not pending:
                continue
            ret = start_claim(source='auto')
            if ret.get('started'):
                print(f"[epic] 自动领取已触发: {', '.join(ret.get('items', []))}", flush=True)
            elif ret.get('reason'):
                print(f"[epic] 自动领取跳过: {ret['reason']}", flush=True)
        except Exception as exc:    # noqa: BLE001 调度线程不允许死
            print(f'[epic] 调度循环异常: {exc}', flush=True)
    print('[epic] 定时调度已退出（Epic喜加一卸载）', flush=True)


def start_scheduler():
    """启动定时调度（幂等）。由 stats_server 启动闸 / 模块安装拉起。"""
    global _sched_started
    if _sched_started:
        return
    _sched_started = True
    _sched_stop.clear()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print('[epic] Epic 周免检测调度已启动（缓存过期补检 + 自动领取）', flush=True)


def stop_scheduler():
    """停止定时调度（游戏专区卸载时调用）。"""
    global _sched_started
    if not _sched_started:
        return
    _sched_started = False
    _sched_stop.set()
