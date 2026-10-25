/**
 * 小宇工具箱 · 游戏专区「Epic 每周免费游戏」前端交互逻辑
 * - 当前免费 / 即将免费 / 领取记录三段展示（数据源 /api/games/epic）
 * - 立即检测、单卡/一键领取（POST claim 打开 WebView2 半自动领取窗口）
 * - 自动领取开关；领取进行中每 3s 轮询进度
 * - 与 app.js 的约定：window.initGamesPage / window.loadEpic
 */

(function () {
  'use strict';

  var epicState = { data: null, pollTimer: null, lastAction: '' };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(msg, isError) {
    var box = document.getElementById('gamesToastNotice');
    if (!box) {
      box = document.createElement('div');
      box.id = 'gamesToastNotice';
      box.style.cssText = 'position:fixed;top:28px;right:28px;z-index:999999;padding:12px 20px;'
        + 'border-radius:8px;font-size:13px;font-weight:500;box-shadow:0 8px 24px rgba(0,0,0,0.5);'
        + 'transition:all 0.25s ease;display:flex;align-items:center;gap:8px;pointer-events:none;';
      document.body.appendChild(box);
    }
    box.style.background = isError ? '#ef4444' : '#10b981';
    box.style.color = '#fff';
    box.innerHTML = (isError ? '<i class="ph ph-warning-circle" style="font-size:16px;"></i> '
                             : '<i class="ph ph-check-circle" style="font-size:16px;"></i> ') + esc(msg);
    box.style.opacity = '1';
    box.style.transform = 'translateY(0)';
    clearTimeout(box._timer);
    box._timer = setTimeout(function () {
      box.style.opacity = '0';
      box.style.transform = 'translateY(-10px)';
    }, 4000);
  }

  function fmtTime(iso) {
    if (!iso) return '--';
    try {
      var d = new Date(iso);
      return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' +
             String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    } catch (e) { return iso; }
  }

  var STATUS_BADGE = {
    unclaimed: { text: '可领取', cls: 'epic-badge-free' },
    claimed:   { text: '已领取', cls: 'epic-badge-owned' },
    attempted: { text: '今日已尝试', cls: 'epic-badge-attempted' },
    manual:    { text: '需手动', cls: 'epic-badge-attempted' }
  };

  function renderCurrent(state) {
    var grid = document.getElementById('epicCurrentGrid');
    if (!grid) return;
    var items = state.current || [];
    if (!items.length) {
      grid.innerHTML = '<div class="epic-empty">当前没有检测到免费游戏，点右上角「立即检测」更新。</div>';
      return;
    }
    grid.innerHTML = items.map(function (it) {
      var badge = STATUS_BADGE[it.status] || STATUS_BADGE.unclaimed;
      var claimBtn = '';
      if (!state.claiming.running) {
        if (it.status === 'unclaimed') {
          claimBtn = '<button class="btn-main epic-claim-one" data-offer="' + esc(it.id) + '">'
            + '<i class="ph ph-download-simple"></i>领取</button>';
        } else if (it.status === 'attempted') {
          claimBtn = '<button class="btn-secondary epic-claim-one" style="background:#f59e0b;color:#000;font-weight:600;" data-offer="' + esc(it.id) + '" title="自动重试受限，点此手动重试">'
            + '<i class="ph ph-arrow-clockwise"></i>重试</button>';
        }
      }
      return '<div class="epic-card">'
        + '<a href="' + esc(it.url) + '" target="_blank" rel="noopener" class="epic-card-img">'
        + (it.image ? '<img src="' + esc(it.image) + '" loading="lazy" alt="">' : '')
        + '</a>'
        + '<div class="epic-card-body">'
        + '<div class="epic-card-title" title="' + esc(it.title) + '">' + esc(it.title) + '</div>'
        + '<div class="epic-card-time"><i class="ph ph-clock"></i>'
        + esc(fmtTime(it.start)) + ' → ' + esc(fmtTime(it.end)) + '</div>'
        + '<div class="epic-card-foot">'
        + '<span class="epic-badge ' + badge.cls + '">' + badge.text + '</span>'
        + claimBtn
        + '</div></div></div>';
    }).join('');
  }

  function renderUpcoming(state) {
    var list = document.getElementById('epicUpcomingList');
    if (!list) return;
    var items = state.upcoming || [];
    if (!items.length) {
      list.innerHTML = '<div class="epic-empty">官方还没公布下周的免费游戏。</div>';
      return;
    }
    list.innerHTML = items.map(function (it) {
      return '<div class="epic-up-item">'
        + (it.image ? '<img src="' + esc(it.image) + '" loading="lazy" alt="">' : '')
        + '<div><div class="epic-up-title">' + esc(it.title) + '</div>'
        + '<div class="epic-up-time">开抢 ' + esc(fmtTime(it.start)) + ' · 截止 '
        + esc(fmtTime(it.end)) + '</div></div></div>';
    }).join('');
  }

  function renderHistory(state) {
    var list = document.getElementById('epicHistoryList');
    if (!list) return;
    var items = state.history || [];
    if (!items.length) {
      list.innerHTML = '<div class="epic-empty">还没有领取记录。</div>';
      return;
    }
    list.innerHTML = items.map(function (h) {
      return '<div class="epic-his-item"><i class="ph ph-check-circle"></i>'
        + '<span class="epic-his-title">' + esc(h.title) + '</span>'
        + '<span class="epic-his-time">' + esc(fmtTime(h.claimedAt)) + '</span></div>';
    }).join('');
  }

  function renderProgress(state) {
    var bar = document.getElementById('epicClaimProgress');
    if (!bar) return;
    if (state.claiming && state.claiming.running) {
      var done = (state.claiming.progress || []).filter(function (p) {
        return p.status === 'claimed' || p.status === 'owned';
      }).length;
      var cur = (state.claiming.progress || []).filter(function (p) {
        return p.status !== 'pending' && p.status !== 'claimed' && p.status !== 'owned';
      })[0];
      bar.hidden = false;
      bar.innerHTML = '<i class="ph ph-spinner-gap epic-spin"></i>领取窗口运行中（'
        + done + '/' + (state.claiming.progress || []).length + ' 完成）'
        + (cur ? ' · 当前：' + esc(cur.title) + '（' + esc(cur.status) + '）' : '')
        + ' · 需要登录或验证码时请在窗口中手动完成';
    } else {
      bar.hidden = true;
      bar.innerHTML = '';
    }
  }

  function renderStateText(state) {
    var el = document.getElementById('epicStateText');
    if (!el) return;
    var t = '上次检测：' + (state.lastCheck ? fmtTime(state.lastCheck) : '从未');
    if (state.lastCheckError) t += ' · 检测失败：' + state.lastCheckError;
    el.textContent = t;
  }

  function renderAuto(state) {
    var tg = document.getElementById('epicAutoToggle');
    if (tg && document.activeElement !== tg) tg.checked = !!state.auto;
  }

  function renderAll(state) {
    epicState.data = state;
    renderCurrent(state);
    renderUpcoming(state);
    renderHistory(state);
    renderProgress(state);
    renderStateText(state);
    renderAuto(state);
    // 领取进行中：3s 轮询直到结束
    if (state.claiming && state.claiming.running) {
      if (!epicState.pollTimer) {
        epicState.pollTimer = setInterval(function () {
          if (location.hash !== '#/games') { stopPoll(); return; }
          loadEpic();
        }, 3000);
      }
    } else {
      stopPoll();
    }
  }

  function stopPoll() {
    if (epicState.pollTimer) {
      clearInterval(epicState.pollTimer);
      epicState.pollTimer = null;
    }
  }

  window.loadEpic = function () {
    fetch('/api/games/epic', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(renderAll)
      .catch(function (e) { console.error('[games] 状态加载失败', e); });
  };

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function doClaim(offerIds) {
    epicState.lastAction = 'claim';
    postJson('/api/games/epic/claim', offerIds ? { offerIds: offerIds } : {})
      .then(function (ret) {
        if (!ret.ok) { toast(ret.reason || '领取未启动', true); }
        else { toast('领取窗口已打开：' + (ret.items || []).join('、')); }
        loadEpic();
      })
      .catch(function () { toast('领取请求失败', true); });
  }

  window.initGamesPage = function () {
    var refreshBtn = document.getElementById('btnEpicRefresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.dataset.bound = '1';
      refreshBtn.addEventListener('click', function () {
        refreshBtn.disabled = true;
        postJson('/api/games/epic/refresh', {})
          .then(function (ret) {
            toast(ret.ok ? '检测完成' : '检测失败：' + (ret.error || ''), !ret.ok);
            loadEpic();
          })
          .catch(function () { toast('检测请求失败', true); })
          .finally(function () { refreshBtn.disabled = false; });
      });
    }
    var claimBtn = document.getElementById('btnEpicClaim');
    if (claimBtn && !claimBtn.dataset.bound) {
      claimBtn.dataset.bound = '1';
      claimBtn.addEventListener('click', function () { doClaim(null); });
    }
    var autoToggle = document.getElementById('epicAutoToggle');
    if (autoToggle && !autoToggle.dataset.bound) {
      autoToggle.dataset.bound = '1';
      autoToggle.addEventListener('change', function () {
        postJson('/api/games/epic/auto', { enabled: autoToggle.checked })
          .then(function (ret) {
            toast(ret.ok ? (ret.auto ? '自动领取已开启' : '自动领取已关闭') : '设置失败', !ret.ok);
          })
          .catch(function () { toast('设置请求失败', true); });
      });
    }
    // 单卡领取按钮（列表重渲染后事件仍在容器上，用事件委托）
    var grid = document.getElementById('epicCurrentGrid');
    if (grid && !grid.dataset.bound) {
      grid.dataset.bound = '1';
      grid.addEventListener('click', function (ev) {
        var btn = ev.target.closest ? ev.target.closest('.epic-claim-one') : null;
        if (btn) doClaim([btn.getAttribute('data-offer')]);
      });
    }
  };
})();
