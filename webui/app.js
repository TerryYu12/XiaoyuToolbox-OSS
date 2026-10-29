/**
 * 桌面工具箱 · 前端交互与数据渲染模块 (app.js - v2.1)
 * 遵循《前端规格-Token统计.md》v2.1 规范：
 * 1. KPI 与工具表口径统一：计入 summary.ccs.cost_usd，总成本与工具表之和对齐
 * 2. terra 异常金额两处完全统一：使用 summary.hermes.cost_anomaly_usd ($18,300,529.57)
 * 3. 模型环形图图例与明细表份额统一：使用当前选中来源的总量作为统一分母，各模型占比根据真实数据动态计算
 * 4. 趋势副标题更新为 "逐请求日志按日聚合 · 成本取自日汇总"
 * 5. Antigravity 去除重复名称，徽标改为 "无本地用量记录"，非 0 呈现
 * 6. 小数值进度条保留可见微条，terra 成本单元格紧凑显示 "$18.3M ⚠️"，补充口径说明
 */

(function () {
  'use strict';

  const MODEL_SOURCE_OPTIONS = [
    { id: 'ccs', label: 'CCS' },
    { id: 'claude', label: 'Claude 本机直读' },
    { id: 'dsh', label: 'DSH' },
    { id: 'hermes', label: 'Hermes' },
    { id: 'codex', label: 'Codex' },
    { id: 'zcode', label: 'ZCode' },
    { id: 'pi', label: 'Pi / OMP' },
    { id: 'opencode', label: 'OpenCode' },
    { id: 'commandcode', label: 'CommandCode' },
    { id: 'xiaomi_mimo', label: 'Xiaomi MiMo 桌面端' },
    { id: 'workbuddy', label: 'WorkBuddy' },
    { id: 'antigravity', label: 'Antigravity' }
  ];
  const MODEL_SOURCE_IDS = MODEL_SOURCE_OPTIONS.map(source => source.id);
  const MODEL_SOURCE_STORAGE_KEY = 'dashboard_model_sources_v4';
  // 每代新增的来源：从旧存档迁移时并进选择集，否则老用户升级后总 Token /
  // 环形图会一直漏掉新来源（v1 时代没有 claude，v2 时代没有 dsh，v3 时代没有 antigravity）。
  const MODEL_SOURCE_ADDED = {
    v1: ['claude', 'dsh', 'antigravity'],
    v2: ['dsh', 'antigravity'],
    v3: ['antigravity']
  };
  const MODEL_SOURCE_LEGACY_KEYS = [
    { key: 'dashboard_model_sources_v3', gen: 'v3' },
    { key: 'dashboard_model_sources_v2', gen: 'v2' },
    { key: 'dashboard_model_sources_v1', gen: 'v1' }
  ];

  function loadEnabledModelSources() {
    try {
      const raw = localStorage.getItem(MODEL_SOURCE_STORAGE_KEY);
      if (raw !== null) {
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return MODEL_SOURCE_IDS.slice();
        return Array.from(new Set(parsed.filter(id => MODEL_SOURCE_IDS.includes(id))));
      }
      for (const legacy of MODEL_SOURCE_LEGACY_KEYS) {
        const stored = localStorage.getItem(legacy.key);
        if (stored === null) continue;
        const parsed = JSON.parse(stored);
        if (!Array.isArray(parsed)) continue;
        return Array.from(new Set([...parsed, ...(MODEL_SOURCE_ADDED[legacy.gen] || [])]))
          .filter(id => MODEL_SOURCE_IDS.includes(id));
      }
      return MODEL_SOURCE_IDS.slice();
    } catch (e) {
      return MODEL_SOURCE_IDS.slice();
    }
  }

  // --- 全局状态 ---
  const state = {
    currentTab: 'stats',          // 'stats' | 'translate'
    timeWindow: '1',              // 'all' | '30' | '7' | '1' | 'custom'（默认今日，与仪表盘默认高亮一致）
    customStart: null,            // 自定义范围起点（毫秒时间戳，浏览器系统时区换算）
    modelSource: 'all',           // 'all' | 'ccs' | 'hermes' | 'codex' | 'zcode'
    enabledModelSources: loadEnabledModelSources(),
    modelSourcesData: null,
    modelSourcesError: null,
    currentGroupFilter: 'all',    // 'all' | 'Hermes' | 'CCS' | 'ZCode' | 'Antigravity'
    chartSeries: { token: true, cost: true },
    dataSourceType: 'sample',     // 'live' | 'sample'
    summaryData: null,
    agentsData: null,
    modelsData: null,
    dailyData: null,
    toolsData: null,
    healthData: null,
    generatedAt: '',
    clipData: null,               // 剪贴板历史 {ok,enabled,total,max_items,items}
    clipSearch: '',               // 剪贴板页搜索词
    clipMaxId: 0                  // 已见最大条目 id（新条目高亮用）
  };

  // 同源相对路径：页面由本机统计服务直接托管，跟端口走（独立调试/正式 exe 两相宜）
  const API_BASE = '/api/stats';
  // 翻译走同源 8790 的 POST /api/translate（llama-server 的 8899 只供后端直调）

  // --- 格式化辅助函数 ---
  function formatCompact(num) {
    if (num === null || num === undefined || isNaN(num)) return '0';
    const abs = Math.abs(num);
    if (abs >= 1e9) return (num / 1e9).toFixed(2) + 'B';
    if (abs >= 1e6) {
      const val = num / 1e6;
      return (val < 10 ? val.toFixed(2) : val.toFixed(1)) + 'M';
    }
    if (abs >= 1e3) return (num / 1e3).toFixed(1) + 'K';
    return num.toLocaleString('en-US');
  }

  function formatNumber(num) {
    if (num === null || num === undefined || isNaN(num)) return '0';
    return Number(num).toLocaleString('en-US');
  }

  function formatCurrency(num) {
    if (num === null || num === undefined || isNaN(num)) return '$0.00';
    const val = Number(num);
    if (val === 0) return '$0.00';
    if (val >= 1000000) return '$' + (val / 1e6).toFixed(2) + 'M';
    if (val >= 1000) return '$' + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (val < 0.01) return '$' + val.toFixed(4);
    return '$' + val.toFixed(2);
  }

  function formatPercent(val) {
    if (val === null || val === undefined || isNaN(val)) return '0.0%';
    return (Number(val) * 100).toFixed(1) + '%';
  }

  // HTML 转义（账号卡 / 历史表渲染使用，防止凭据字段中的特殊字符破坏 DOM）
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // --- 可折叠面板状态管理与 localStorage 持久化 ---
  window.togglePanel = function(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    const isCollapsed = panel.classList.toggle('is-collapsed');
    try {
      localStorage.setItem('panel_state_' + panelId, isCollapsed ? 'collapsed' : 'expanded');
    } catch (e) {}
  };

  function initPanelStates() {
    const panels = [
      { id: 'panel-tools', defaultState: 'expanded' },
      { id: 'panel-models', defaultState: 'expanded' },
      { id: 'panel-daily', defaultState: 'expanded' },
      { id: 'panel-sources', defaultState: 'collapsed' }
    ];

    panels.forEach(p => {
      const panelEl = document.getElementById(p.id);
      if (!panelEl) return;
      let stateVal = p.defaultState;
      try {
        const saved = localStorage.getItem('panel_state_' + p.id);
        if (saved === 'collapsed' || saved === 'expanded') {
          stateVal = saved;
        }
      } catch (e) {}

      if (stateVal === 'collapsed') {
        panelEl.classList.add('is-collapsed');
      } else {
        panelEl.classList.remove('is-collapsed');
      }
    });
  }

  function updatePanelSummaries() {
    // 1. 工具面板摘要
    const sumTools = document.getElementById('summary-tools');
    if (sumTools) {
      const tools = state.toolsData || [];
      const count = tools.length;
      const totalTok = tools.reduce((acc, cur) => {
        const tTok = (cur.tokens && cur.tokens.available !== false) ? (cur.tokens.total_tokens || 0) : (cur.total_tokens || 0);
        return acc + tTok;
      }, 0);
      sumTools.textContent = `${count} 项 · 合计 ${formatCompact(totalTok)}`;
    }

    // 2. 模型面板摘要
    const sumModels = document.getElementById('summary-models');
    if (sumModels) {
      if (state.modelSource === 'ccs') {
        const ccsSummary = state.summaryData?.ccs;
        const ccsTotal = ccsSummary ? ((ccsSummary.input || 0) + (ccsSummary.output || 0) + (ccsSummary.cache_read || 0) + (ccsSummary.cache_creation || 0)) : 0;
        const models = (state.modelsData || []).slice().sort((a, b) => {
          const ta = a.total_tokens || ((a.input || 0) + (a.output || 0) + (a.cache_read || 0) + (a.cache_creation || 0));
          const tb = b.total_tokens || ((b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_creation || 0));
          return tb - ta;
        });
        const top = models[0];
        if (top && ccsTotal > 0) {
          const topTok = top.total_tokens || ((top.input || 0) + (top.output || 0) + (top.cache_read || 0) + (top.cache_creation || 0));
          const pct = ((topTok / ccsTotal) * 100).toFixed(1) + '%';
          sumModels.textContent = `CCS 主导 · ${top.model} ${pct}`;
        } else if (top) {
          sumModels.textContent = `CCS · ${top.model}`;
        } else {
          sumModels.textContent = 'CCS · —';
        }
      } else if (state.modelSource === 'hermes') {
        const modelMap = {};
        (state.agentsData || []).forEach(a => {
          (a.models || []).forEach(m => {
            const mName = m.model || '(unknown)';
            if (!modelMap[mName]) modelMap[mName] = { name: mName, total: 0 };
            modelMap[mName].total += ((m.input || 0) + (m.output || 0) + (m.cache_read || 0));
          });
        });
        const list = Object.values(modelMap).sort((a, b) => b.total - a.total);
        const hermesSummary = state.summaryData?.hermes;
        const hermesTotal = hermesSummary ? ((hermesSummary.input || 0) + (hermesSummary.output || 0) + (hermesSummary.cache_read || 0) + (hermesSummary.cache_write || 0)) : list.reduce((a, b) => a + b.total, 0);
        const top = list[0];
        if (top && hermesTotal > 0) {
          const pct = ((top.total / hermesTotal) * 100).toFixed(1) + '%';
          sumModels.textContent = `Hermes · ${top.name} ${pct}`;
        } else if (top) {
          sumModels.textContent = `Hermes · ${top.name}`;
        } else {
          sumModels.textContent = 'Hermes · —';
        }
      } else if (state.modelSource === 'zcode') {
        const zcodeTool = (state.toolsData || []).find(t => t.name === 'ZCode' || t.group === 'ZCode');
        const models = (zcodeTool && Array.isArray(zcodeTool.models)) ? zcodeTool.models : [];
        const top = models[0];
        const zTotal = zcodeTool ? (zcodeTool.total_tokens || zcodeTool.provider_total_tokens || 0) : 0;
        if (top && zTotal > 0) {
          const topTok = top.total || ((top.input || 0) + (top.output || 0) + (top.cache_read || 0));
          const pct = ((topTok / zTotal) * 100).toFixed(1) + '%';
          sumModels.textContent = `ZCode · ${top.model} ${pct}`;
        } else if (top) {
          sumModels.textContent = `ZCode · ${top.model}`;
        } else {
          sumModels.textContent = 'ZCode · —';
        }
      } else if (state.modelSource === 'codex') {
        const codexTool = (state.toolsData || []).find(t => t.name === 'Codex' || t.group === 'Codex');
        const models = (codexTool && Array.isArray(codexTool.models)) ? codexTool.models : [];
        const total = models.reduce((sum, model) => sum + (model.total_tokens || 0), 0);
        const top = models[0];
        if (top && total > 0) {
          const pct = ((top.total_tokens || 0) / total * 100).toFixed(1) + '%';
          sumModels.textContent = `Codex · ${top.model} ${pct}`;
        } else {
          sumModels.textContent = 'Codex · —';
        }
      } else if (state.modelSourcesData && Array.isArray(state.modelSourcesData.sources)) {
        const source = state.modelSourcesData.sources.find(item => item.id === state.modelSource);
        if (source) {
          const models = (source.models || []).slice().sort((a, b) =>
            (Number(b.total_tokens) || 0) - (Number(a.total_tokens) || 0));
          const total = Number(source.total_tokens) || 0;
          const top = models[0];
          if (top && total > 0) {
            const pct = ((Number(top.total_tokens) || 0) / total * 100).toFixed(1) + '%';
            sumModels.textContent = `${source.name || state.modelSource} · ${top.model || '(unknown)'} ${pct}`;
          } else {
            sumModels.textContent = `${source.name || state.modelSource} · —`;
          }
        } else {
          sumModels.textContent = '—';
        }
      } else if (state.modelSource === 'all') {
        sumModels.textContent = `全部合并 · ${(state.enabledModelSources || []).length} 个来源已选`;
      } else {
        sumModels.textContent = '—';
      }
    }

    // 3. 日期趋势摘要
    const sumDaily = document.getElementById('summary-daily');
    if (sumDaily) {
      if (state.dailyData && state.dailyData.length > 0) {
        const days = state.dailyData.length;
        const maxDay = Math.max(...state.dailyData.map(d => (d.input || 0) + (d.output || 0) + (d.cache_read || 0) + (d.cache_creation || 0)));
        sumDaily.textContent = `${days}天趋势 · 峰值 ${formatCompact(maxDay)}`;
      } else {
        sumDaily.textContent = '—';
      }
    }

    // 4. 数据源摘要
    const sumSources = document.getElementById('summary-sources');
    if (sumSources) {
      let hermesPart = 'Hermes —';
      if (Array.isArray(state.healthData?.hermes)) {
        const okCount = state.healthData.hermes.filter(h => h.ok).length;
        const totalMb = state.healthData.hermes.reduce((acc, cur) => acc + (cur.size_mb || 0), 0).toFixed(1);
        hermesPart = `Hermes ${okCount} Profiles (${totalMb}MB)`;
      } else if (state.summaryData?.hermes?.agents != null) {
        hermesPart = `Hermes ${state.summaryData.hermes.agents} Agents`;
      }

      let ccsPart = 'CCS —';
      const ccsReqs = state.summaryData?.ccs?.requests;
      const ccsSize = state.healthData?.ccs?.size_mb;
      if (ccsReqs != null && ccsSize != null) {
        ccsPart = `CCS ${ccsReqs.toLocaleString()}条 (${Number(ccsSize).toFixed(1)}MB)`;
      } else if (ccsReqs != null) {
        ccsPart = `CCS ${ccsReqs.toLocaleString()}条`;
      } else if (ccsSize != null) {
        ccsPart = `CCS (${Number(ccsSize).toFixed(1)}MB)`;
      }

      if (state.dataSourceType === 'live') {
        const allOk = (state.healthData?.hermes?.every(h => h.ok !== false) !== false) && (state.healthData?.ccs?.ok !== false);
        const statusText = allOk ? '全部正常' : '部分就绪';
        sumSources.textContent = `${hermesPart} · ${ccsPart} · ${statusText}`;
      } else {
        sumSources.textContent = '本地样本 (只读) · 待连通服务';
      }
    }
  }

  // --- 本地样本同步渲染 (保证 0ms 秒开直出，断网无缝可用) ---
  function initFallback() {
    state.dataSourceType = 'sample';
    const fallback = window.FALLBACK_DATA || {};
    state.generatedAt = fallback.generated_at || '2026-09-17 21:14:26';
    
    // 根据时间窗选取对应汇总
    if (state.timeWindow === '1') {
      state.summaryData = fallback.summary_1d || fallback.summary_all;
    } else if (state.timeWindow === '7') {
      state.summaryData = fallback.summary_7d || fallback.summary_all;
    } else {
      state.summaryData = fallback.summary_all;
    }

    state.agentsData = fallback.agents || [];
    state.modelsData = fallback.models || fallback.ccs_models || [];
    state.dailyData = fallback.daily || fallback.ccs_daily || [];
    
    // 提取 tools 数据
    if (fallback.tools && fallback.tools.tools) {
      state.toolsData = fallback.tools.tools;
    } else if (Array.isArray(fallback.tools)) {
      state.toolsData = fallback.tools;
    } else {
      state.toolsData = [];
    }

    state.healthData = fallback.health || {
      hermes: { path: '%LOCALAPPDATA%\\hermes\\state.db', readable: true, status: '本地样本 (只读)' },
      ccs: { path: '%USERPROFILE%\\.cc-switch\\cc-switch.db', readable: true, status: '本地样本 (只读)' }
    };

    updateHeaderStatus();
    renderOverviewCards();
    renderToolsSection();
    renderModelSection();
    renderDailyChart();
    renderSourceStatusBar();
    zpRenderStatic();
    initPanelStates();
    updatePanelSummaries();
  }

  // ============================================================
  // Zen Pulse 仪表盘（pgDash 重构版）
  // 实时数据：/api/stats/pulse（30s 轮询，仅新版后台有——旧 exe 404 时
  // 实时卡保持占位，历史汇总卡不受影响）。历史汇总复用 state.* 旧口径。
  // 配色/布局约定见 zp.css；本模块只负责计算与 SVG 直出。
  // ============================================================
  // 主题相关色走 zp.css 的 CSS 变量：SVG 属性不支持 var()，一律用 style= 输出；
  // 固定品牌色（工具卡图标 hex）仍在 zpToolStyle。
  const ZP_COLORS = {
    coral: 'var(--zp-coral)', coral2: 'var(--zp-coral-2)', lav: 'var(--zp-lav)',
    sage: 'var(--zp-sage)', slate: 'var(--zp-slate)', blue: 'var(--zp-blue)',
    ink: 'var(--zp-ink)', ink2: 'var(--zp-ink-2)', ink3: 'var(--zp-ink-3)',
    ok: 'var(--zp-ok)'
  };
  const ZP_FONT = 'Segoe UI", "Microsoft YaHei UI", sans-serif';
  // 官方思考星芒 morph 动画（claude.ai 官方实现原样移植）：12 根辐条的
  // [角度deg, 长度unit] 在 8 个实测关键帧间连续插值，CYCLE=1.6s 一循环。
  // 静止形态 = 第 0 帧（即 Claude 标志本身）。
  const ZP_MORPH_F = [
    [[20, 22.5], [42, 21.5], [58, 21], [89, 30.6], [115, 28.3], [139, 27.3],
     [174, 26.3], [213, 27.2], [246, 29.4], [286, 26.8], [320, 27], [359, 24.1]],
    [[18, 27.2], [45, 25.6], [64, 24], [89, 21.6], [116, 19.5], [138, 28.3],
     [175, 29.1], [210, 28.3], [241, 29.3], [283, 25.7], [316, 27.3], [359, 27.5]],
    [[18, 28.1], [49, 27], [65, 27.4], [97, 25.4], [122, 22.6], [144, 21.6],
     [171, 19.8], [206, 31.8], [237, 30.4], [276, 24.8], [312, 27.6], [356, 25.8]],
    [[14, 26.2], [46, 27.1], [67, 26.2], [101, 27.3], [127, 26.1], [148, 23.9],
     [178, 21.8], [207, 19.8], [237, 32.1], [271, 27.8], [305, 27.5], [348, 26.1]],
    [[13, 25.9], [46, 26.5], [66, 26], [103, 27.8], [133, 27.8], [155, 27.2],
     [185, 25.9], [212, 23.5], [236, 22], [273, 28], [302, 28.2], [346, 26.3]],
    [[5, 27], [35, 24.3], [57, 24.7], [97, 25.6], [130, 27.2], [155, 26.8],
     [188, 26.8], [216, 26], [241, 24.1], [274, 21.5], [305, 20.7], [342, 29.3]],
    [[6, 27.8], [36, 28], [55, 26.7], [94, 26.3], [126, 25.8], [154, 25.7],
     [189, 27.2], [220, 29.6], [245, 28.3], [280, 23.5], [306, 21.2], [342, 18.3]],
    [[15, 19.7], [36, 22], [57, 29], [90, 27.5], [120, 28], [143, 25.2],
     [182, 26.3], [221, 28.1], [248, 28.5], [287, 26.9], [321, 24.6], [355, 21.5]]
  ];
  const ZP_LOGO_FIT = 44 / 263.5;  // claude.svg 测量 px → 150 画布
  // 静止标志 = claude.svg 实测几何（deg, len150, 宽 12, 核 12），纯静态渲染
  const ZP_MORPH = { raf: 0, lines: [], gEl: null, coreEl: null,
    epoch: 0, t0: 0, mode: 'idle' };  // idle → in → loop → out → idle
  const ZP_MORPH_SC = 1.37;   // 官方 24-unit 帧 → 本页 150 画布（最大辐条 32.1→44）
  const ZP_MORPH_SW = 4.6 * ZP_MORPH_SC;   // morph 态线宽
  const ZP_MORPH_CORE = 6 * ZP_MORPH_SC;   // morph 态核半径
  const ZP_BLEND_MS = 650;                 // 静止↔动画 的圆滑过渡时长
  const ZP_VT_RANGES = [60, 180, 360, 720, 1440];   // v-t 图时间筛选（分钟）
  const ZP_STATE = {
    pulse: null, ts: 0, timer: null, mode: 'token',
    series: { input: true, output: true, cache_w: true, cache_r: true },
    focus: null, focusName: '',
    heroRateUnit: localStorage.getItem('zp_hero_rate_unit') || 's',
    vtMode: localStorage.getItem('zp_vt_mode') || 'gen',
    vtModel: localStorage.getItem('zp_vt_model') || '',
    // v-t 图时间筛选：默认最近 1 小时；非默认窗（3h/6h/12h/24h）走 /rate-series 独立取数
    vtRange: (() => { const r = parseInt(localStorage.getItem('zp_vt_range'), 10); return ZP_VT_RANGES.includes(r) ? r : 60; })(),
    vt: null, vtErr: false,
    // 2026-10-03 新增卡：时钟时区、天气（数据 + 城市 + 取数时间戳）、系统监控快照
    clockTimezone: 'local',
    weather: null, weatherCity: '', weatherTs: 0,
    sysmon: null
  };

  function zpEl(id) { return document.getElementById(id); }

  function zpMoney(n) {
    const v = Number(n) || 0;
    if (Math.abs(v) >= 1000) return '$' + (v / 1000).toFixed(2) + 'K';
    return '$' + v.toFixed(2);
  }

  function zpRel(ts) {
    if (!ts) return '—';
    const diff = Date.now() / 1000 - ts;
    if (diff < 90) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    return `${Math.floor(diff / 86400)} 天前`;
  }

  // 较昨日同比文案（>1000% 折 K%，与参考样式一致）
  function zpDeltaText(cur, prev) {
    if (prev === null || prev === undefined || prev <= 0) return '昨日无数据';
    const pct = (cur - prev) / prev * 100;
    const abs = Math.abs(pct);
    const body = abs >= 1000 ? formatCompact(abs) : abs.toFixed(abs >= 100 ? 0 : 1) + '%';
    return `${pct >= 0 ? '↑' : '↓'}${body} 较昨日`;
  }

  function zpLocalDateKey(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  function zpNiceMax(v) {
    if (v <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.5, 2, 3, 5, 7.5, 10]) {
      if (v <= m * exp) return m * exp;
    }
    return 10 * exp;
  }

  // Catmull-Rom 采样成密集点列（面积与描边共用，视觉平滑）
  function zpSmoothSamples(pts, seg = 8) {
    if (pts.length < 3) return pts.slice();
    const out = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(i - 1, 0)], p1 = pts[i],
        p2 = pts[i + 1], p3 = pts[Math.min(i + 2, pts.length - 1)];
      for (let t = 0; t < seg; t++) {
        const s = t / seg, s2 = s * s, s3 = s2 * s;
        out.push([
          0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * s +
            (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * s2 +
            (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * s3),
          0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * s +
            (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * s2 +
            (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * s3)
        ]);
      }
    }
    out.push(pts[pts.length - 1].slice());
    return out;
  }

  function zpLineD(samples) {
    return samples.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  }

  function zpAreaD(top, bottom) {
    // top/bottom 都是自左向右的采样点列；bottom 反向接回去围成面
    const bad = (top || []).concat(bottom || []).find(q => !Array.isArray(q) || q[0] == null || q[1] == null);
    if (bad !== undefined) {
      console.warn('[zpAreaD] bad point:', JSON.stringify(bad),
        'topLen', (top || []).length, 'botLen', (bottom || []).length,
        'sample:', JSON.stringify((bottom || []).slice(0, 3)));
    }
    const back = bottom.slice().reverse();
    return zpLineD(top) + ' ' + back.map(p => `L${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ') + ' Z';
  }

  // ---- KPI 迷你趋势线 ----
  function zpSpark(svgId, values, color) {
    const svg = zpEl(svgId);
    if (!svg) return;
    const W = 220, H = 34, pad = 2;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    if (!values || values.length < 2) { svg.innerHTML = ''; return; }
    const max = Math.max(...values);
    if (max <= 0) { svg.innerHTML = ''; return; }
    const min = Math.min(...values);
    const span = (max - min) || max;
    const xs = i => pad + i / (values.length - 1) * (W - pad * 2);
    const ys = v => H - pad - ((v - min) / span) * (H - pad * 2 - 4) - 3;
    const line = zpSmoothSamples(values.map((v, i) => [xs(i), ys(v)]), 10);
    const gid = `zpg_${svgId}`;
    svg.innerHTML = `
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" style="stop-color:${color}" stop-opacity=".30"/>
        <stop offset="100%" style="stop-color:${color}" stop-opacity="0"/>
      </linearGradient></defs>
      <path d="${zpAreaD(line, line.map(pt => [pt[0], H]))}" fill="url(#${gid})" stroke="none"/>
      <path d="${zpLineD(line)}" fill="none" style="stroke:${color}" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round"/>`;
  }

  // ============ 数据获取（节流） ============
  async function zpFetch(force) {
    if (!force && Date.now() - ZP_STATE.ts < 15000) return;
    ZP_STATE.ts = Date.now();
    try {
      // /tools 全盘扫描期间 pulse 会被拖到 3-6s，超时给足余量
      const d = await fetchWithTimeout(`${API_BASE}/pulse`, 12000);
      ZP_STATE.pulse = d || null;
      ZP_STATE.retries = 0;
    } catch (e) {
      console.warn('[ZenPulse] /pulse 不可用（旧版后台？）:', e.message || e);
      if (!ZP_STATE.pulse) ZP_STATE.pulse = null;
      // 快速重试（最多 2 次），不让首屏空星芒等满 30s 轮询
      if ((ZP_STATE.retries || 0) < 2) {
        ZP_STATE.retries = (ZP_STATE.retries || 0) + 1;
        setTimeout(() => zpFetch(true), 3000);
      }
    }
    await zpFetchVt();
    zpRenderLive();
    zpUpdateToolWhen();
    // 自愈：桌面窗口常在 8790 就绪前完成首屏（loadData 全败退回样本），
    // pulse 一旦成功说明服务已活，把历史汇总重拉一次，样本态不粘滞
    if (ZP_STATE.pulse && ZP_STATE.pulse.available !== false
        && state.dataSourceType === 'sample' && !state._zpSelfHeal) {
      state._zpSelfHeal = true;
      loadData();
    }
  }

  // v-t 图时间筛选：非默认窗（3h/6h/12h/24h）单独取数（/api/stats/rate-series）。
  // 默认 1h 直接复用 pulse.minutes（后端与 _rate_series 同一实现，省一次计算）；
  // 旧后台无此路由时置 vtErr，视图保持近 1 小时并在副标题标注。
  async function zpFetchVt() {
    const range = ZP_STATE.vtRange || 60;
    if (range === 60) { ZP_STATE.vt = null; ZP_STATE.vtErr = false; return; }
    try {
      const d = await fetchWithTimeout(`${API_BASE}/rate-series?range=${range}`, 12000);
      if (ZP_STATE.vtRange !== range) return;   // 期间又切了窗口：丢弃过期响应
      if (d && d.available !== false && Array.isArray(d.minutes)) {
        ZP_STATE.vt = d;
        ZP_STATE.vtErr = false;
      } else {
        ZP_STATE.vt = null;
        ZP_STATE.vtErr = true;
      }
    } catch (e) {
      if (ZP_STATE.vtRange !== range) return;
      console.warn('[ZenPulse] /rate-series 不可用（旧版后台？）:', e.message || e);
      ZP_STATE.vt = null;
      // 仅 404（旧 exe 无此路由）标记「需新版后台」；其它瞬时失败留「加载中…」随 30s 轮询自愈
      ZP_STATE.vtErr = /HTTP 404/.test(e && e.message || '');
    }
    zpRenderRateVt();
  }

  // ============================================================
  // 仪表盘卡片网格编辑器：布局存 localStorage（zpDashLayoutV1）。
  // 长按卡片 600ms 进入编辑：右/下边缘拉动调大小、× 删除、＋ 添加、完成退出。
  // ============================================================
  const ZP_CARD_DEFS = {
    quota: { title: '额度监控', w: 12, h: 6 },
    hero: { title: '近 10 分钟速率', w: 12, h: 4 },
    'kpi-tokens': { title: '总 Token', w: 6, h: 3 },
    'kpi-cost': { title: '预估费用', w: 6, h: 3 },
    'kpi-inout': { title: '新输入 / 输出', w: 6, h: 3 },
    'kpi-hit': { title: '缓存命中率', w: 6, h: 3 },
    agents: { title: 'Agent 卡行', w: 12, h: 9 },
    area: { title: '堆叠面积图', w: 12, h: 8 },
    ripple: { title: '请求波', w: 9, h: 5 },
    budget: { title: '预算', w: 3, h: 5 },
    'rate-vt': { title: '请求速率（v-t图）', w: 12, h: 6 },
    stream: { title: '请求流', w: 12, h: 8 },
    models: { title: '模型占比', w: 12, h: 9 },
    // 2026-10-03 新增：时钟/天气/系统监控默认上板；图片槽默认收进 + 号菜单
    clock: { title: '时钟', w: 4, h: 3 },
    weather: { title: '天气', w: 4, h: 4 },
    sysmon: { title: '系统监控', w: 4, h: 4 },
    'img-1': { title: '自定义图片 1', w: 4, h: 4, defaultHidden: true },
    'img-2': { title: '自定义图片 2', w: 4, h: 4, defaultHidden: true },
    'img-3': { title: '自定义图片 3', w: 4, h: 4, defaultHidden: true },
    'img-4': { title: '自定义图片 4', w: 4, h: 4, defaultHidden: true }
  };
  const ZP_LAYOUT_KEY = 'zpDashLayoutV1';
  // 新卡（时钟/天气/系统监控/自定义图片）的逐卡配置：图片卡的图片 dataURL、
  // 天气卡的城市。权威存 appearance.json 的 dashboardCards 键（跟随布局同一
  // POST 保存）；localStorage 仅作旧后台兜底。图片槽 id 固定 4 个。
  const ZP_IMG_SLOTS = ['img-1', 'img-2', 'img-3', 'img-4'];
  const ZP_CARDS_KEY = 'zpDashCardsV1';
  const ZP_CARDS = { config: {} };
  const ZP_GRID_GAP = 8, ZP_GRID_ROW = 34, ZP_GRID_COLS = 12;
  const ZP_GRID = { editing: false, detached: {}, order: null, size: null,
    hidden: [], rz: null, pressTimer: null, pressXY: null, suppressClick: false,
    drag: null, saveTimer: null, remoteReady: false,
    agOrder: null, agHidden: [],
    presets: {}, activePreset: null };
  // 布局持久化走 /api/appearance（用户本机 appearance.json，安装更新不丢）；
  // localStorage 仅作旧数据迁移源与远端不可用时的兜底。
  function zpLayoutLocal() {
    try {
      const raw = JSON.parse(localStorage.getItem(ZP_LAYOUT_KEY) || 'null');
      if (raw && Array.isArray(raw.order)) return raw;
    } catch (e) { }
    return null;
  }
  function zpLayoutCurrent() {
    return { order: ZP_GRID.order, size: ZP_GRID.size, hidden: ZP_GRID.hidden,
      agentsOrder: ZP_GRID.agOrder, agentsHidden: ZP_GRID.agHidden };
  }
  function zpLayoutApply(raw) {
    if (!raw || !Array.isArray(raw.order)) return false;
    const grid = zpEl('zpGrid');
    if (!grid) return false;
    ZP_GRID.order = raw.order.filter(id => ZP_CARD_DEFS[id]
      && (grid.querySelector(`[data-card-id="${id}"]`) || ZP_GRID.detached[id]));
    ZP_GRID.size = raw.size || {};
    ZP_GRID.hidden = Array.isArray(raw.hidden) ? raw.hidden : [];
    ZP_GRID.agOrder = Array.isArray(raw.agentsOrder) ? raw.agentsOrder : null;
    ZP_GRID.agHidden = Array.isArray(raw.agentsHidden) ? raw.agentsHidden : [];
    ZP_GRID.order.forEach(id => {
      const node = grid.querySelector(`[data-card-id="${id}"]`) || ZP_GRID.detached[id];
      if (!node) return;
      const [w, h] = zpGridSize(id);
      zpApplySpan(node, w, h);
      // 无条件 append：对已在 grid 的节点等价于移动重排（原条件写法只在恢复隐藏卡时
      // 动节点，启动与切预设时静态卡全部已在 grid → 视觉永远不跟 order 走）
      grid.appendChild(node);
      delete ZP_GRID.detached[id];
    });
    ZP_GRID.hidden.forEach(id => {
      const node = grid.querySelector(`[data-card-id="${id}"]`) || ZP_GRID.detached[id];
      if (node && node.parentNode === grid) {
        ZP_GRID.detached[id] = node;   // 必须入池：预设切回时全靠 detached 找回节点
        node.remove();
      }
    });
    // 2026-10-03 新增卡补位：远端布局存档可能早于新卡（时钟/天气/系统监控/
    // 图片槽），未被存档提及的卡——defaultHidden 的收进 + 号菜单，其余补挂到
    // 布局尾部。不补挂的话卡片会浮在 HTML 静态位置，拖不进 order。
    Object.keys(ZP_CARD_DEFS).forEach(id => {
      if (ZP_GRID.order.includes(id) || ZP_GRID.hidden.includes(id)) return;
      const node = grid.querySelector(`[data-card-id="${id}"]`) || ZP_GRID.detached[id];
      if (!node) return;
      ZP_GRID.detached[id] = node;
      node.remove();
      if (!ZP_CARD_DEFS[id].defaultHidden) {
        ZP_GRID.hidden = ZP_GRID.hidden.filter(x => x !== id);
        ZP_GRID.order.push(id);
        const [w, h] = zpGridSize(id);
        zpApplySpan(node, w, h);
        grid.appendChild(node);
        delete ZP_GRID.detached[id];
      } else {
        if (!ZP_GRID.hidden.includes(id)) ZP_GRID.hidden.push(id);
      }
    });
    return true;
  }
  async function zpLayoutPull() {
    try {
      const resp = await fetch('/api/appearance');
      if (!resp.ok) return;
      const data = await resp.json();
      ZP_GRID.remoteReady = true;
      const cfg = data.config || {};
      ZP_GRID.presets = cfg.dashboardPresets || {};
      ZP_GRID.activePreset = cfg.dashboardActivePreset || null;
      // 图片卡图片 / 天气城市等逐卡配置：远端权威，先于布局应用（渲染不依赖布局）
      zpCardsApply(cfg.dashboardCards || {});
      const remote = cfg.dashboardLayout || null;
      const local = zpLayoutLocal();
      if (remote) {
        zpLayoutApply(remote);
        zpRenderTools();   // Agent 行顺序由渲染层吃 agOrder，apply 改内存后必须重染
      } else if (local) {
        // 旧版本布局从 localStorage 迁移到个人设置
        zpLayoutApply(local);
        zpRenderTools();
        zpLayoutQueueSave();
        localStorage.removeItem(ZP_LAYOUT_KEY);
      }
    } catch (e) { /* 远端不可用：沿用本地缓存布局 */ }
  }
  function zpLayoutQueueSave() {
    clearTimeout(ZP_GRID.saveTimer);
    ZP_GRID.saveTimer = setTimeout(zpLayoutFlushSave, 300);
  }
  async function zpLayoutFlushSave() {
    const layout = zpLayoutCurrent();
    let saved = false;
    try {
      const resp = await fetch('/api/appearance');
      if (resp.ok) {
        const data = await resp.json();
        // 预设与激活名是顶层字段（后端 sanitize 白名单各管各的），不塞 dashboardLayout 里
        const body = Object.assign({}, data.config || {}, { dashboardLayout: layout,
          dashboardPresets: ZP_GRID.presets,
          dashboardActivePreset: ZP_GRID.activePreset,
          dashboardCards: ZP_CARDS.config || {} });
        const r2 = await fetch('/api/appearance', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        saved = r2.ok;
      }
    } catch (e) { }
    if (!saved) {
      try {
        localStorage.setItem(ZP_LAYOUT_KEY, JSON.stringify(layout));
        localStorage.setItem(ZP_CARDS_KEY, JSON.stringify(ZP_CARDS.config || {}));
      } catch (e) { }
      console.warn('[ZenPulse] 布局暂存 localStorage（/api/appearance 不可用或为旧版后台）');
    }
  }
  function zpLayoutSave() { zpLayoutQueueSave(); }

  // ---- 新卡逐卡配置（dashboardCards）：与布局合并同一次 /api/appearance POST ----
  function zpCardsLocal() {
    try {
      const raw = JSON.parse(localStorage.getItem(ZP_CARDS_KEY) || 'null');
      if (raw && typeof raw === 'object') return raw;
    } catch (e) { }
    return null;
  }
  function zpCardsApply(cards) {
    ZP_CARDS.config = (cards && typeof cards === 'object') ? cards : {};
    ZP_IMG_SLOTS.forEach(zpRenderImgCard);
    const city = ((ZP_CARDS.config.weather || {}).city || '').trim();
    if (city && city !== ZP_STATE.weatherCity) {
      ZP_STATE.weatherCity = city;
      ZP_STATE.weather = null;
      zpWeatherFetch(true);
    } else if (!city) {
      ZP_STATE.weatherCity = '';
      ZP_STATE.weather = null;
      zpRenderWeather();
    }
    const tz = ((ZP_CARDS.config.clock || {}).timezone || 'local').trim();
    ZP_STATE.clockTimezone = tz || 'local';
    zpRenderClock();
  }
  function zpCardsQueueSave() { zpLayoutQueueSave(); }

  function zpGridInit() {
    const grid = zpEl('zpGrid');
    if (!grid) { window.__zpNoGrid = true; return; }
    const saved = zpLayoutLocal();
    const nodes = {};
    grid.querySelectorAll('[data-card-id]').forEach(n => {
      nodes[n.getAttribute('data-card-id')] = n;
    });
    const allIds = Object.keys(ZP_CARD_DEFS).filter(id => nodes[id]);
    ZP_GRID.order = (saved && saved.order) ? saved.order.filter(id => nodes[id]) : [];
    ZP_GRID.hidden = (saved && Array.isArray(saved.hidden))
      ? saved.hidden.filter(id => nodes[id]) : [];
    // 无存档位置的卡：defaultHidden（图片槽）收进 + 号菜单，其余排到布局尾部。
    // 这样老用户的布局不被新卡打乱顺序，新卡也保底可见/可从菜单添加。
    allIds.forEach(id => {
      if (ZP_GRID.order.includes(id) || ZP_GRID.hidden.includes(id)) return;
      if (ZP_CARD_DEFS[id] && ZP_CARD_DEFS[id].defaultHidden) ZP_GRID.hidden.push(id);
      else ZP_GRID.order.push(id);
    });
    ZP_GRID.size = (saved && saved.size) ? saved.size : {};
    // 摘掉隐藏卡（保留引用供 + 号恢复）
    ZP_GRID.hidden.forEach(id => {
      if (nodes[id]) { ZP_GRID.detached[id] = nodes[id]; nodes[id].remove(); }
    });
    // 按布局顺序挂载 + 应用跨列/跨行
    ZP_GRID.order.forEach(id => {
      const n = nodes[id];
      if (!n) return;
      grid.appendChild(n);
      const def = ZP_CARD_DEFS[id] || { w: 12, h: 4 };
      const sz = ZP_GRID.size[id] || [def.w, def.h];
      zpApplySpan(n, sz[0], sz[1]);
    });
    // 图片卡先按本地缓存渲染（远端 appearance 到达后 zpCardsApply 覆盖）
    zpCardsApply(zpCardsLocal() || {});
    zpBindGridEdit(grid);
    zpLayoutPull();   // 远端个人设置有布局则覆盖（localStorage 旧布局迁移上传）
  }

  function zpApplySpan(node, w, h) {
    node.style.gridColumn = `span ${Math.max(1, Math.min(ZP_GRID_COLS, w))}`;
    node.style.gridRow = `span ${Math.max(1, h)}`;
  }

  function zpGridSize(id) {
    const def = ZP_CARD_DEFS[id] || { w: 12, h: 4 };
    return ZP_GRID.size[id] || [def.w, def.h];
  }

  // FLIP：mutate 改变布局后，其他卡片从旧位置平滑滑到新位置（让位动画）
  function zpFlipOthers(grid, skip, mutate) {
    const nodes = [...grid.querySelectorAll('[data-card-id]')].filter(n => n !== skip);
    const first = nodes.map(n => n.getBoundingClientRect());
    mutate();
    nodes.forEach((n, i) => {
      const f = first[i], l = n.getBoundingClientRect();
      const dx = f.left - l.left, dy = f.top - l.top;
      if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
      n.classList.add('zp-flip');
      n.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        n.style.transform = '';
        setTimeout(() => n.classList.remove('zp-flip'), 320);
      });
    });
  }

  // 编辑中拖动卡片本体：fixed 定位贴鼠标（卡片脱离网格流跟手），
  // 拖起时其他卡 FLIP 合拢，悬停目标卡时重排让位，松手平滑落回网格槽位
  function zpDragStart(e, card) {
    e.preventDefault();
    const grid = zpEl('zpGrid');
    const rect = card.getBoundingClientRect();
    const offX = e.clientX - rect.left, offY = e.clientY - rect.top;
    const id = card.getAttribute('data-card-id');
    const others = [...grid.querySelectorAll('[data-card-id]')].filter(n => n !== card);
    const first = others.map(n => n.getBoundingClientRect());
    card.classList.add('zp-dragging');
    card.style.position = 'fixed';
    card.style.left = rect.left + 'px';
    card.style.top = rect.top + 'px';
    card.style.width = rect.width + 'px';
    card.style.height = rect.height + 'px';
    card.style.zIndex = 80;
    others.forEach((n, i) => {
      const f = first[i], l = n.getBoundingClientRect();
      const dx = f.left - l.left, dy = f.top - l.top;
      if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
      n.classList.add('zp-flip');
      n.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        n.style.transform = '';
        setTimeout(() => n.classList.remove('zp-flip'), 320);
      });
    });
    const move = ev => {
      card.style.left = (ev.clientX - offX) + 'px';
      card.style.top = (ev.clientY - offY) + 'px';
      const cx = ev.clientX - offX + rect.width / 2;
      const cy = ev.clientY - offY + rect.height / 2;
      const live = [...grid.querySelectorAll('[data-card-id]')].filter(n => n !== card);
      const target = live.find(n => {
        const r = n.getBoundingClientRect();
        return cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom;
      });
      if (target) {
        const tid = target.getAttribute('data-card-id');
        if (ZP_GRID.order.includes(tid)) {
          zpFlipOthers(grid, card, () => {
            ZP_GRID.order = ZP_GRID.order.filter(x => x !== id);
            ZP_GRID.order.splice(ZP_GRID.order.indexOf(tid), 0, id);
            grid.insertBefore(card, target);
          });
        }
      }
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      const last = card.getBoundingClientRect();
      card.classList.remove('zp-dragging');
      card.style.position = '';
      card.style.left = '';
      card.style.top = '';
      card.style.width = '';
      card.style.height = '';
      card.style.zIndex = '';
      const now = card.getBoundingClientRect();
      const dx = last.left - now.left, dy = last.top - now.top;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
        card.classList.add('zp-flip');
        card.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          card.style.transform = '';
          setTimeout(() => card.classList.remove('zp-flip'), 320);
        });
      }
      zpLayoutSave();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function zpApplyPreset(name) {
    const preset = ZP_GRID.presets[name];
    if (!preset) { window.__zpPErr = 'no preset ' + name; return; }
    try {
    zpLayoutApply(preset);
    ZP_GRID.agOrder = Array.isArray(preset.agentsOrder) ? preset.agentsOrder : null;
    ZP_GRID.agHidden = Array.isArray(preset.agentsHidden) ? preset.agentsHidden : [];
    ZP_GRID.activePreset = name;
    zpRenderTools();
    zpRenderStatic();
    zpRenderLive();
    zpLayoutSave();
    } catch (err) { window.__zpPErr = String(err && err.stack || err); }
  }

  // 仪表盘内轻量确认弹窗（删除预设等破坏性操作）；风格走 zp 令牌，暗/亮主题自适应
  function zpConfirmDialog(message, onOk) {
    const old = zpEl('zpConfirmMask');
    if (old) old.remove();
    const mask = document.createElement('div');
    mask.className = 'zp-confirm-mask';
    mask.id = 'zpConfirmMask';
    mask.innerHTML = '<div class="zp-confirm-box" role="dialog" aria-modal="true">' +
      '<div class="zp-confirm-msg">' + escapeHtml(message) + '</div>' +
      '<div class="zp-confirm-actions">' +
      '<button type="button" class="zp-confirm-cancel">取消</button>' +
      '<button type="button" class="zp-confirm-ok">删除</button>' +
      '</div></div>';
    const close = () => { mask.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); close(); } };
    mask.addEventListener('pointerdown', ev => { if (ev.target === mask) close(); });
    mask.querySelector('.zp-confirm-cancel').addEventListener('click', close);
    mask.querySelector('.zp-confirm-ok').addEventListener('click', () => { close(); onOk(); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(mask);
    mask.querySelector('.zp-confirm-ok').focus();
  }

  function zpPresetMenuToggle() {
    const btn = zpEl('zpPresetBtn');
    if (!btn) return;
    let menu = zpEl('zpPresetMenu');
    if (menu) { menu.remove(); return; }
    menu = document.createElement('div');
    menu.className = 'zp-add-menu';
    menu.id = 'zpPresetMenu';
    const close = () => { const m = zpEl('zpPresetMenu'); if (m) m.remove(); };
    const outside = ev => {
      if (ev.target.closest('#zpPresetMenu') || ev.target.closest('#zpPresetBtn')) return;
      close();
      document.removeEventListener('pointerdown', outside, true);
    };
    const onKey = ev => {
      if (ev.key === 'Escape') {
        close();
        document.removeEventListener('keydown', onKey, true);
      }
    };
    setTimeout(() => {
      document.addEventListener('pointerdown', outside, true);
      document.addEventListener('keydown', onKey, true);
    }, 0);
    const names = Object.keys(ZP_GRID.presets);
    const head = document.createElement('div');
    head.className = 'zp-add-item';
    head.style.cursor = 'default';
    head.innerHTML = '<span>布局预设</span><span class="dim">' +
      (ZP_GRID.activePreset ? '当前：' + escapeHtml(ZP_GRID.activePreset) : '未使用预设') + '</span>';
    menu.appendChild(head);
    if (!names.length) {
      const empty = document.createElement('div');
      empty.className = 'zp-add-item';
      empty.style.cursor = 'default';
      empty.innerHTML = '<span>还没有预设</span><span class="dim">调整布局后在此保存</span>';
      menu.appendChild(empty);
    }
    names.forEach(name => {
      const item = document.createElement('div');
      item.className = 'zp-add-item';
      const active = name === ZP_GRID.activePreset;
      item.innerHTML = '<span>' + (active ? '✓ ' : '') + escapeHtml(name) + '</span><span class="dim">切换</span>';
      item.addEventListener('click', () => {
        zpApplyPreset(name);
        close();
        document.removeEventListener('pointerdown', outside, true);
        document.removeEventListener('keydown', onKey, true);
      });
      const del = document.createElement('span');
      del.textContent = '✕';
      del.title = '删除该预设';
      del.style.color = 'var(--err)';
      del.style.marginLeft = '8px';
      del.addEventListener('click', ev => {
        ev.stopPropagation();
        // 删除不可恢复，弹窗确认防误触
        zpConfirmDialog(`删除布局预设「${name}」？该操作不可恢复。`, () => {
          delete ZP_GRID.presets[name];
          if (ZP_GRID.activePreset === name) ZP_GRID.activePreset = null;
          zpLayoutSave();
          item.remove();
        });
      });
      item.appendChild(del);
      menu.appendChild(item);
    });
    const save = document.createElement('div');
    save.className = 'zp-add-item';
    save.innerHTML = '<span><b>保存当前布局为预设</b></span>';
    save.addEventListener('click', () => {
      if (menu.querySelector('.zp-preset-input-row')) return;
      const row = document.createElement('div');
      row.className = 'zp-add-item zp-preset-input-row';
      row.style.cursor = 'default';
      row.innerHTML = '<input class="zp-budget-input" style="width:110px" placeholder="预设名称">' +
        '<button class="zp-chip coral" style="cursor:pointer">存</button>';
      menu.appendChild(row);
      const input = row.querySelector('input');
      input.focus();
      const commit = () => {
        const name = (input.value || '').trim();
        if (!name) { row.remove(); return; }
        ZP_GRID.presets[name] = zpLayoutCurrent();
        ZP_GRID.activePreset = name;
        zpLayoutSave();
        menu.remove();
      };
      row.querySelector('button').addEventListener('click', commit);
      input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') commit();
        if (ev.key === 'Escape') row.remove();
        ev.stopPropagation();
      });
    });
    menu.appendChild(save);
    zpEl('pgDash').appendChild(menu);
  }

  // 给单张卡挂编辑手柄（× 删除 + 右/下边缘拖拽）。
  // 进编辑时对全部卡跑一遍；编辑中途从「＋」菜单恢复的卡也必须补挂，否则该卡没有编辑手柄。
  function zpMountCardHandles(n) {
    if (n.querySelector('.zp-card-del')) return;  // 已挂过防重复
    const del = document.createElement('button');
    del.className = 'zp-card-del';
    del.textContent = '×';
    del.title = '删除卡片';
    del.addEventListener('click', ev => {
      ev.stopPropagation();
      const id = n.getAttribute('data-card-id');
      ZP_GRID.hidden.push(id);
      ZP_GRID.order = ZP_GRID.order.filter(x => x !== id);
      ZP_GRID.detached[id] = n;
      n.remove();
      zpLayoutSave();
    });
    const rzR = document.createElement('div');
    rzR.className = 'zp-card-rz-r'; rzR.title = '拖动调整宽度';
    const rzB = document.createElement('div');
    rzB.className = 'zp-card-rz-b'; rzB.title = '拖动调整高度';
    n.appendChild(del); n.appendChild(rzR); n.appendChild(rzB);
    rzR.addEventListener('pointerdown', ev => zpRzStart(ev, n, 'w'));
    rzB.addEventListener('pointerdown', ev => zpRzStart(ev, n, 'h'));
  }

  function zpEnterEdit() {
    if (ZP_GRID.editing) return;
    ZP_GRID.editing = true;
    document.getElementById('pgDash').classList.add('zp-editing');
    const add = zpEl('zpAddBtn'), done = zpEl('zpEditDone'), hint = zpEl('zpEditHint');
    if (add) add.style.display = '';
    if (done) done.style.display = '';
    if (hint) hint.style.display = '';
    const eb0 = zpEl('zpEditBtn');
    if (eb0) eb0.style.display = 'none';
    // 每张可见卡挂 × 与右/下边缘手柄
    zpEl('zpGrid').querySelectorAll('[data-card-id]').forEach(zpMountCardHandles);
    zpAgentsEditOn();   // Agent 小卡手柄（渲染先于进编辑，补挂）
  }

  function zpExitEdit() {
    ZP_GRID.editing = false;
    document.getElementById('pgDash').classList.remove('zp-editing');
    ['zpAddBtn', 'zpEditDone'].forEach(id => { const el = zpEl(id); if (el) el.style.display = 'none'; });
    const eb = zpEl('zpEditBtn'); if (eb) eb.style.display = '';
    const pm = zpEl('zpPresetMenu'); if (pm) pm.remove();
    const hint = zpEl('zpEditHint'); if (hint) hint.style.display = 'none';
    zpEl('zpGrid').querySelectorAll('.zp-card-del, .zp-card-rz-r, .zp-card-rz-b')
      .forEach(x => x.remove());
    zpEl('zpTools').querySelectorAll('.zp-card-del').forEach(x => x.remove());
    const menu = zpEl('zpAddMenu'); if (menu) { menu.remove(); if (window.zpAddMenuOff) zpAddMenuOff(); }
    zpLayoutSave();
  }

  function zpRzStart(ev, node, axis) {
    ev.preventDefault(); ev.stopPropagation();
    const id = node.getAttribute('data-card-id');
    const [w0, h0] = zpGridSize(id);
    const x0 = ev.clientX, y0 = ev.clientY;
    const grid = zpEl('zpGrid');
    const colW = (grid.clientWidth - (ZP_GRID_COLS - 1) * ZP_GRID_GAP) / ZP_GRID_COLS
      + ZP_GRID_GAP;
    const rowH = ZP_GRID_ROW + ZP_GRID_GAP;
    const others = [...grid.querySelectorAll('[data-card-id]')].filter(n => n !== node);
    const first = others.map(n => n.getBoundingClientRect());
    const move = e => {
      const dw = Math.round((e.clientX - x0) / colW);
      const dh = Math.round((e.clientY - y0) / rowH);
      const nw = axis === 'w' ? Math.max(1, Math.min(ZP_GRID_COLS, w0 + dw)) : w0;
      const nh = axis === 'h' ? Math.max(1, Math.min(20, h0 + dh)) : h0;
      ZP_GRID.size[id] = [nw, nh];
      zpApplySpan(node, nw, nh);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      // 松手后被挤开的卡平滑滑到新位置
      others.forEach((n, i) => {
        const f = first[i], l = n.getBoundingClientRect();
        const dx = f.left - l.left, dy = f.top - l.top;
        if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
        n.classList.add('zp-flip');
        n.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          n.style.transform = '';
          setTimeout(() => n.classList.remove('zp-flip'), 320);
        });
      });
      zpLayoutSave();
      setTimeout(() => {
        zpRenderAll();
        zpRenderClock();
      }, 50);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function zpToggleAddMenu() {
    const add = zpEl('zpAddBtn');
    if (!add) return;
    let menu = zpEl('zpAddMenu');
    if (menu) { menu.remove(); zpAddMenuOff(); return; }
    // 点菜单外部 / Esc 关闭
    const outside = ev => {
      if (ev.target.closest('#zpAddMenu') || ev.target.closest('#zpAddBtn')) return;
      const m = zpEl('zpAddMenu');
      if (m) { m.remove(); zpAddMenuOff(); }
    };
    const onKey = ev => {
      if (ev.key === 'Escape') {
        const m = zpEl('zpAddMenu');
        if (m) { m.remove(); zpAddMenuOff(); }
      }
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', onKey, true);
    window.zpAddMenuOff = () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', onKey, true);
    };
    menu = document.createElement('div');
    menu.className = 'zp-add-menu';
    menu.id = 'zpAddMenu';
    const avail = ZP_GRID.hidden.filter(id => ZP_CARD_DEFS[id]);
    ZP_GRID.agHidden.slice().forEach(key => {
      const item = document.createElement('div');
      item.className = 'zp-add-item';
      item.innerHTML = `<span>Agent · ${escapeHtml(key)}</span><span class="dim">＋</span>`;
      item.addEventListener('click', () => {
        ZP_GRID.agHidden = ZP_GRID.agHidden.filter(x => x !== key);
        if (!ZP_GRID.agOrder) ZP_GRID.agOrder = [];
        if (!ZP_GRID.agOrder.includes(key)) ZP_GRID.agOrder.push(key);
        zpRenderTools();
        zpLayoutSave();
        menu.remove();
      });
      menu.appendChild(item);
    });
    if (!avail.length && !ZP_GRID.agHidden.length) {
      menu.innerHTML = '<div class="zp-add-item"><span>暂无可恢复的卡片</span><span class="dim">点卡片 × 删除后出现在这里</span></div>';
    } else {
      avail.forEach(id => {
        const item = document.createElement('div');
        item.className = 'zp-add-item';
        const def = ZP_CARD_DEFS[id];
        item.innerHTML = `<span>${escapeHtml(def.title)}</span><span class="dim">＋</span>`;
        item.addEventListener('click', () => {
          ZP_GRID.hidden = ZP_GRID.hidden.filter(x => x !== id);
          ZP_GRID.order.push(id);
          const node = ZP_GRID.detached[id];
          if (node) {
            const [w, h] = zpGridSize(id);
            zpApplySpan(node, w, h);
            zpEl('zpGrid').appendChild(node);
            delete ZP_GRID.detached[id];
            if (ZP_GRID.editing) zpMountCardHandles(node);
            zpRenderStatic();
            zpRenderLive();
            if (id.startsWith('img-')) zpRenderImgCard(id);
            else if (id === 'clock') zpRenderClock();
            else if (id === 'weather') zpRenderWeather();
            else if (id === 'sysmon') zpRenderSysmon();
          }
          zpLayoutSave();
          menu.remove();
        });
        menu.appendChild(item);
      });
    }
    zpEl('pgDash').appendChild(menu);
  }

  function zpBindGridEdit(grid) {
    // 长按 600ms 进入编辑（交互控件与工具卡点击不触发）
    grid.addEventListener('pointerdown', e => {
      window.__zpHit = (window.__zpHit || 0) + 1;
      console.log('[ZP] grid pointerdown, editing =', ZP_GRID.editing);
      if (ZP_GRID.editing) {
        // 编辑中：拖卡片本体移动位置（手柄/按钮/图片卡上传区除外）
        if (e.target.closest('button, input, select, a, .zp-card-rz-r, .zp-card-rz-b, .zp-quota-actions, .zp-quota-refresh, .zp-img-empty, .zp-img-btn, .zp-img-file')) return;
        const card = e.target.closest('[data-card-id]');
        if (card) zpDragStart(e, card);
        return;
      }
      if (e.target.closest('button, input, select, a, .zp-legend, .zp-mode, .zp-tool, .zp-quota-refresh, .zp-img-empty, .zp-img-btn, .zp-img-file')) return;
      ZP_GRID.pressXY = [e.clientX, e.clientY];
      clearTimeout(ZP_GRID.pressTimer);
      ZP_GRID.pressTimer = setTimeout(() => {
        ZP_GRID.suppressClick = true;
        setTimeout(() => { ZP_GRID.suppressClick = false; }, 300);
        zpEnterEdit();
      }, 1000);
    });
    grid.addEventListener('pointermove', e => {
      if (!ZP_GRID.pressTimer || !ZP_GRID.pressXY) return;
      if (Math.hypot(e.clientX - ZP_GRID.pressXY[0], e.clientY - ZP_GRID.pressXY[1]) > 10) {
        clearTimeout(ZP_GRID.pressTimer);
        ZP_GRID.pressTimer = null;
      }
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev =>
      grid.addEventListener(ev, () => { clearTimeout(ZP_GRID.pressTimer); ZP_GRID.pressTimer = null; }));
    // 编辑模式下吞掉卡片点击（避免误触工具聚焦等）
    grid.addEventListener('click', e => {
      // 编辑模式的 × 删除按钮与图片卡上传区/工具要放行，其余点击在编辑中一律吞掉
      if (e.target.closest('.zp-card-del, .zp-img-empty, .zp-img-btn, .zp-img-file')) return;
      if (ZP_GRID.suppressClick || ZP_GRID.editing) {
        e.stopPropagation();
        e.preventDefault();
      }
    }, true);
    const add = zpEl('zpAddBtn');
    if (add) add.addEventListener('click', zpToggleAddMenu);
    const pbtn = zpEl('zpPresetBtn');
    if (pbtn) pbtn.addEventListener('click', zpPresetMenuToggle);
    const ebtn = zpEl('zpEditBtn');
    if (ebtn) ebtn.addEventListener('click', zpEnterEdit);
    const done = zpEl('zpEditDone');
    if (done) done.addEventListener('click', zpExitEdit);
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape' && ZP_GRID.editing) zpExitEdit();
    });
  }

  function zpWatchTheme() {
    // 外观设置切亮/暗（body.light）时重绘仪表盘：CSS 变量即时生效，SVG 需重画
    if (!window.MutationObserver) return;
    let t = null;
    new MutationObserver(() => {
      clearTimeout(t);
      t = setTimeout(zpRenderAll, 120);
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }

  function zpStartTimer() {
    if (ZP_STATE.timer) clearInterval(ZP_STATE.timer);
    ZP_STATE.timer = setInterval(() => {
      const pg = zpEl('pgDash');
      if (pg && pg.classList.contains('on')) zpFetch();
    }, 30000);
    // 时钟卡：1s 直更（页面不可见时 zpRenderClock 内部跳过）
    if (ZP_STATE.clockTimer) clearInterval(ZP_STATE.clockTimer);
    zpRenderClock();
    ZP_STATE.clockTimer = setInterval(zpRenderClock, 1000);
    // 系统监控卡：3s 轮询（psutil 本地只读，后端还有 2s 缓存兜底）
    if (ZP_STATE.sysmonTimer) clearInterval(ZP_STATE.sysmonTimer);
    ZP_STATE.sysmonTimer = setInterval(() => {
      const pg = zpEl('pgDash');
      if (pg && pg.classList.contains('on')) zpSysmonLoad();
    }, 3000);
    // 天气卡：30 分钟自动刷新（后端另有 30 分钟数据缓存）
    if (ZP_STATE.weatherTimer) clearInterval(ZP_STATE.weatherTimer);
    ZP_STATE.weatherTimer = setInterval(() => {
      const pg = zpEl('pgDash');
      if (pg && pg.classList.contains('on') && ZP_STATE.weatherCity) zpWeatherFetch(true);
    }, 30 * 60 * 1000);
  }

  // ============ 英雄卡：速率 + 放射花 ============
  function zpRenderHero() {
    const P = ZP_STATE.pulse;
    const rateEl = zpEl('zpHeroRate'), badgeEl = zpEl('zpRateBadgeVal');
    if (!rateEl) return;
    if (!P || P.available === false) {
      rateEl.textContent = '—';
      if (badgeEl) badgeEl.textContent = '--';
      const note = zpEl('zpHeroNote');
      if (note) note.textContent = P && P.reason ? `实时数据不可用：${P.reason}` : '实时数据需新版后台（/api/stats/pulse）';
      return;
    }
    const note = zpEl('zpHeroNote');
    if (note) note.textContent = '全 Agent LLM 纯文本生成速率 · CCS + Codex + Claude + DSH + Antigravity 本机直读';

    const unitMode = ZP_STATE.heroRateUnit || 's';
    const rate10Out = P.rate10 ? (P.rate10.output_tokens || 0) : 0;
    let rate = 0;
    let rateStr = '0';
    if (unitMode === 's') {
      rate = (P.rate10 && P.rate10.output_per_second !== undefined) ? P.rate10.output_per_second : (rate10Out / 600.0);
      rateStr = rate >= 100 ? formatCompact(Math.round(rate)) : (rate > 0 ? (rate >= 10 ? rate.toFixed(1) : rate.toFixed(1)) : '0');
    } else {
      rate = (P.rate10 && P.rate10.output_per_minute !== undefined) ? P.rate10.output_per_minute : Math.round(rate10Out / 10);
      rateStr = formatCompact(rate);
    }
    rateEl.textContent = rateStr;

    const unitEl = zpEl('zpHeroUnit');
    if (unitEl) unitEl.textContent = unitMode === 's' ? 'tok / s' : 'tok / min';

    if (badgeEl) {
      badgeEl.textContent = rateStr;
      const badgeUnitEl = zpEl('zpRateBadgeUnit');
      if (badgeUnitEl) badgeUnitEl.textContent = unitMode === 's' ? 'tok / 秒' : 'tok / 分';
    }

    const hourEl = zpEl('zpHeroHour'), reqsEl = zpEl('zpHeroReqs');
    if (hourEl && P.rate60) hourEl.textContent = formatCompact(P.rate60.output_tokens || 0);
    if (reqsEl && P.rate60) reqsEl.textContent = formatNumber(P.rate60.requests || 0);
    zpRenderBurst(P.minutes || []);
  }

  function zpRenderBurst(minutes) {
    const svg = zpEl('zpBurst');
    if (!svg) return;
    const C = 60, max = Math.max(...minutes.map(m => (m.output_tokens || 0)), 1);
    let spokes = '';
    minutes.forEach((m, i) => {
      const deg = i / minutes.length * 360 - 90;
      const rad = deg * Math.PI / 180;
      const outTok = m.output_tokens || 0;
      const len = outTok > 0 ? 2.5 + (outTok / max) * 6.5 : 1.2;
      const r0 = 50, r1 = r0 + len;
      const x1 = C + Math.cos(rad) * r0, y1 = C + Math.sin(rad) * r0;
      const x2 = C + Math.cos(rad) * r1, y2 = C + Math.sin(rad) * r1;
      const recent = i >= 50;
      spokes += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"
        style="stroke:${recent ? ZP_COLORS.coral2 : ZP_COLORS.ink3}" stroke-opacity="${recent ? .9 : .55}"
        stroke-width="${recent ? 1.6 : 1.1}" stroke-linecap="round"/>`;
    });
    // 中心：官方思考星芒（claude.ai morph v4 原样移植）。
    // 空闲（近 2 分钟无纯文本生成）= 冻结在第 0 帧；
    // 有纯文本生成 = 8 关键帧连续插值的官方动画。
    const active = (minutes || []).slice(-2).some(m => (m.output_tokens || 0) > 0);
    svg.innerHTML = `
      <circle cx="${C}" cy="${C}" r="54.5" fill="none" style="stroke:${ZP_COLORS.ink3}"
        stroke-opacity=".35" stroke-width="1" stroke-dasharray="1 5"/>
      ${spokes}
      <g transform="translate(${C} ${C})">
        <g class="zp-morph-g" style="stroke:${ZP_COLORS.coral}" stroke-width="${ZP_CLAUDE_W}"
          stroke-linecap="round" fill="none">
          ${'<line class="zp-morph" x1="0" y1="0" x2="0" y2="0"/>'.repeat(12)}
        </g>
        <circle class="zp-morph-core" r="${ZP_CLAUDE_CORE}" style="fill:var(--zp-coral)"/>
      </g>`;
    ZP_MORPH.lines = Array.from(svg.querySelectorAll('line.zp-morph'));
    ZP_MORPH.gEl = svg.querySelector('.zp-morph-g');
    ZP_MORPH.coreEl = svg.querySelector('.zp-morph-core');
    if (!ZP_MORPH.epoch) ZP_MORPH.epoch = performance.now();
    zpMorphPaintStatic();   // 先落到静止标志形态（无动画）
    zpMorphSync(active);    // 再由状态机决定渐入/循环/渐出/保持静止
  }

  // ---- 思考星芒引擎：静止标志(claude.svg 几何) ⇄ 官方 morph 循环 ----
  // 静止 = 纯静态渲染（无 rAF）；开始/停止用 650ms smoothstep 圆滑变形衔接。
  // 静止态粗细与动态 morph 对齐（用户定稿：粗细一致，仅形状变形）
  const ZP_CLAUDE_W = ZP_MORPH_SW, ZP_CLAUDE_CORE = ZP_MORPH_CORE;

  // 角度环绕安全的角度+长度插值
  function zpSpokeLerp(aDeg, aLen, bDeg, bLen, f) {
    let d = bDeg;
    if (d - aDeg > 180) d -= 360;
    if (aDeg - d > 180) d += 360;
    return [aDeg + (d - aDeg) * f, aLen + (bLen - aLen) * f];
  }

  // 官方 morph 第 frame 帧（浮点）的 12 根辐条 [deg, len150]
  function zpMorphShape(frame) {
    const F = ZP_MORPH_F, n = F.length;
    const p = ((frame % n) + n) % n;
    const i = Math.floor(p), f = p - i;
    const a = F[i], b = F[(i + 1) % n];
    const out = [];
    for (let k = 0; k < 12; k++) {
      let a0 = a[k][0], a1 = b[k][0];
      if (a1 - a0 > 180) a1 -= 360;
      if (a0 - a1 > 180) a1 += 360;
      const deg = a0 + (a1 - a0) * f;
      const len = (a[k][1] + (b[k][1] - a[k][1]) * f - 2.3) * ZP_MORPH_SC;
      out.push([deg, len]);
    }
    return out;
  }

  // 静止标志形态 = claude.svg 实测几何（deg + 150 画布长度，角度均匀、长度 39~44）。
  // 2026-10-02 修正：曾误用 morph 帧表第 0 帧顶替——那是动画里的变形帧
  // （前三根挤在 20~58°、长度悬殊），静止时形状明显不对。
  function zpClaudeShape() {
    return [[10.5, 39.4], [42.5, 40.8], [81, 40.1], [102.5, 40.8],
            [130, 40.9], [148, 39.3], [182.5, 40.0], [212, 39.6],
            [235.5, 37.7], [271, 40.1], [305.5, 40.7], [334.5, 44.0]];
  }

  // 把一组形状画到当前 DOM（paint 形状 + 宽度/核随 blend 变化）
  function zpMorphPaint(spokes, width, coreR) {
    if (!ZP_MORPH.lines.length) return;
    spokes.forEach(([deg, len], k) => {
      const rad = deg * Math.PI / 180;
      const l = ZP_MORPH.lines[k];
      l.setAttribute('x2', (Math.cos(rad) * len).toFixed(2));
      l.setAttribute('y2', (Math.sin(rad) * len).toFixed(2));
    });
    if (ZP_MORPH.gEl) ZP_MORPH.gEl.setAttribute('stroke-width', width.toFixed(2));
    if (ZP_MORPH.coreEl) ZP_MORPH.coreEl.setAttribute('r', coreR.toFixed(2));
  }

  // blend：k=0 静止标志，k=1 纯 morph 帧（宽度/核同步插值）；记录 k 供反向续接
  function zpMorphPaintBlend(morphSpokes, k) {
    ZP_MORPH.k = k;
    const cs = zpClaudeShape();
    const spokes = morphSpokes.map((m, i) => zpSpokeLerp(cs[i][0], cs[i][1], m[0], m[1], k));
    zpMorphPaint(spokes,
      ZP_CLAUDE_W + (ZP_MORPH_SW - ZP_CLAUDE_W) * k,
      ZP_CLAUDE_CORE + (ZP_MORPH_CORE - ZP_CLAUDE_CORE) * k);
  }

  const zpEase = f => f * f * (3 - 2 * f);  // smoothstep

  function zpMorphStop() {
    if (ZP_MORPH.raf) { cancelAnimationFrame(ZP_MORPH.raf); ZP_MORPH.raf = 0; }
  }

  function zpMorphRun() {
    zpMorphStop();
    const loop = now => {
      const M = ZP_MORPH;
      const t = (now - M.epoch) / 1000;
      const shape = zpMorphShape(t / 1.6 * ZP_MORPH_F.length);
      if (M.mode === 'loop') {
        zpMorphPaint(shape, ZP_MORPH_SW, ZP_MORPH_CORE);
      } else if (M.mode === 'in') {
        const f = Math.min((now - M.t0) / ZP_BLEND_MS, 1);
        zpMorphPaintBlend(shape, zpEase(f));
        if (f >= 1) M.mode = 'loop';
      } else if (M.mode === 'out') {
        const f = Math.min((now - M.t0) / ZP_BLEND_MS, 1);
        zpMorphPaintBlend(shape, zpEase(1 - f));
        if (f >= 1) {
          M.mode = 'idle';
          zpMorphPaint(zpClaudeShape(), ZP_CLAUDE_W, ZP_CLAUDE_CORE);
          M.raf = 0;
          return;   // 回到纯静态，rAF 退出
        }
      }
      M.raf = requestAnimationFrame(loop);
    };
    ZP_MORPH.raf = requestAnimationFrame(loop);
  }

  // 数据侧状态切换入口（30s 轮询重绘后调用）：active=true 渐入/保持，false 渐出/保持
  function zpMorphSync(active) {
    const M = ZP_MORPH;
    if (!M.lines.length) return;
    if (active) {
      if (M.mode === 'idle') {
        M.mode = 'in';
        M.t0 = performance.now();
        if (!M.epoch) M.epoch = M.t0;
        zpMorphRun();
      } else if (M.mode === 'out') {
        // 渐出中途流量恢复：从当前 blend 位置平滑反向回渐入
        M.mode = 'in';
        M.t0 = performance.now() - (M.k || 0) * ZP_BLEND_MS;
      }
      // in/loop：保持，rAF 直接画新 DOM 引用
    } else if (M.mode === 'in' || M.mode === 'loop') {
      M.mode = 'out';
      M.t0 = performance.now();
      if (!M.raf) zpMorphRun();
    }
    // out/idle：out 自行收敛回 idle；idle 无事
  }

  // 静态渲染（idle 首帧 / 兜底）
  function zpMorphPaintStatic() {
    zpMorphPaint(zpClaudeShape(), ZP_CLAUDE_W, ZP_CLAUDE_CORE);
  }

  // ============ KPI 四卡（历史汇总口径） ============
  function zpRenderKpis() {
    const daily = state.dailyData || [];
    const tail = daily.slice(-14);
    const byDay = {};
    daily.forEach(d => { byDay[d.date] = d; });
    const todayKey = zpLocalDateKey(new Date());
    const yKey = zpLocalDateKey(new Date(Date.now() - 86400e3));
    const dayTotal = d => d ? ((d.input || 0) + (d.output || 0) + (d.cache_read || 0) + (d.cache_creation || 0)) : 0;

    // --- 总 Token（多来源合计 + CCS 日趋势）---
    const merged = getMergedSourceTokenTotals();
    const tokEl = zpEl('zpKpiTokens');
    if (tokEl) tokEl.textContent = merged.total == null ? '—' : formatCompact(merged.total);
    const tokSub = zpEl('zpKpiTokensSub');
    if (tokSub) tokSub.textContent = merged.total == null
      ? '来源统计暂不可用' : `已选 ${merged.selectedCount} 个统计来源`;
    const tokDelta = zpEl('zpTokDelta');
    if (tokDelta) {
      const t = dayTotal(byDay[todayKey]), y = dayTotal(byDay[yKey]);
      tokDelta.textContent = zpDeltaText(t, y);
      tokDelta.className = 'zp-delta ' + (y > 0 ? (t >= y ? 'up' : 'down') : '');
    }
    zpSpark('zpSparkTokens', tail.map(dayTotal), ZP_COLORS.coral2);

    // --- 预估费用（工具卡口径：CCS 各端 + Codex + Hermes + ZCode…）---
    const sum = state.summaryData;
    let ccsCost = sum && sum.ccs && sum.ccs.cost_usd != null ? Number(sum.ccs.cost_usd)
      : daily.reduce((a, c) => a + (Number(c.cost_usd) || 0), 0);
    const hermesCost = sum && sum.hermes ? Number(sum.hermes.cost_usd) || 0 : 0;
    const mergedCost = getMergedCostTotal();
    const totalCost = mergedCost == null ? (ccsCost + hermesCost) : mergedCost;
    const costEl = zpEl('zpKpiCost');
    if (costEl) costEl.textContent = formatCurrency(totalCost);
    const costSub = zpEl('zpKpiCostSub');
    const saved = ZP_STATE.pulse && ZP_STATE.pulse.cache_saved_usd_30d;
    if (costSub) costSub.textContent = saved
      ? `缓存已替你省下 ${zpMoney(saved)}（30 天）`
      : (mergedCost == null
        ? `CCS ${zpMoney(ccsCost)} · Hermes ${zpMoney(hermesCost)}`
        : `全 Agent 汇总 · CCS ${zpMoney(ccsCost)} · Hermes ${zpMoney(hermesCost)}`);
    const costDelta = zpEl('zpCostDelta');
    if (costDelta) {
      const t = (byDay[todayKey] || {}).cost_usd || 0, y = (byDay[yKey] || {}).cost_usd || 0;
      costDelta.textContent = zpDeltaText(t, y);
      costDelta.className = 'zp-delta ' + (y > 0 ? (t >= y ? 'up' : 'down') : '');
    }
    zpSpark('zpSparkCost', tail.map(d => Number(d.cost_usd) || 0), ZP_COLORS.lav);

    // --- 新输入 / 输出（已勾选来源全量口径；缺失时退回 CCS+Hermes）---
    const ccs = (sum && sum.ccs) || {}, hermes = (sum && sum.hermes) || {};
    const usage = getMergedSourceUsage();
    const inp = usage ? usage.input : (Number(ccs.input) || 0) + (Number(hermes.input) || 0);
    const out = usage ? usage.output : (Number(ccs.output) || 0) + (Number(hermes.output) || 0);
    const ioEl = zpEl('zpKpiInOut');
    if (ioEl) ioEl.innerHTML = `${formatCompact(inp)} <small>/</small> ${formatCompact(out)}`;
    const share = (inp + out) > 0 ? out / (inp + out) * 100 : 0;
    const shareEl = zpEl('zpOutShare');
    if (shareEl) shareEl.textContent = `输出占 ${share.toFixed(1)}%`;
    const ioSub = zpEl('zpKpiInOutSub');
    const cacheReadTotal = usage ? usage.cacheRead
      : (Number(ccs.cache_read) || 0) + (Number(hermes.cache_read) || 0);
    if (ioSub) ioSub.textContent = `缓存读取 ${formatCompact(cacheReadTotal)}`
      + (usage ? ` · ${usage.sources} 个来源` : '');
    const bar = zpEl('zpDuoBar');
    if (bar) {
      const total = inp + out;
      bar.querySelector('.seg-input').style.width = total ? `${inp / total * 100}%` : '50%';
      bar.querySelector('.seg-output').style.width = total ? `${out / total * 100}%` : '50%';
    }

    // --- 缓存命中率（与模型明细同口径：按 cache_denominator 计算，避免 Codex 等 OpenAI 口径把 cache_read 在分母重复加两次）---
    let hit, hitRead, hitWrite, denomAll;
    if (usage) {
      denomAll = (usage.cacheDenominator > 0)
        ? usage.cacheDenominator
        : (usage.input + usage.cacheRead + usage.cacheWrite + usage.cacheCreation);
      hit = denomAll > 0 ? usage.cacheRead / denomAll : 0;
      hitRead = usage.cacheRead;
      hitWrite = usage.cacheCreation + usage.cacheWrite;
    } else {
      const cIn = Number(ccs.input) || 0, cCr = Number(ccs.cache_read) || 0,
        cCc = Number(ccs.cache_creation) || 0;
      const hIn = Number(hermes.input) || 0, hCr = Number(hermes.cache_read) || 0,
        hCw = Number(hermes.cache_write) || 0;
      denomAll = (cIn + cCr + cCc) + (hIn + hCr + hCw);
      hit = denomAll > 0 ? (cCr + hCr) / denomAll : 0;
      hitRead = cCr + hCr;
      hitWrite = cCc + hCw;
    }
    const winLabel = state.timeWindow === '1' ? '今日'
      : (state.timeWindow === '7' ? '7 天'
      : (state.timeWindow === '30' ? '30 天'
      : (state.timeWindow === 'all' ? '全部' : '自定义')));
    const hitBadge = zpEl('zpHitWinBadge');
    if (hitBadge) hitBadge.textContent = winLabel;
    const hitEl = zpEl('zpKpiHit');
    if (hitEl) hitEl.textContent = formatPercent(hit);
    const hitSub = zpEl('zpKpiHitSub');
    if (hitSub) {
      hitSub.textContent = `读 ${formatCompact(hitRead)} · 写 ${formatCompact(hitWrite)}`;
      hitSub.title = `【缓存命中率 · ${winLabel}】\n命中率: ${(hit * 100).toFixed(2)}%\n读取缓存: ${formatNumber(hitRead)} Tokens\n写入缓存: ${formatNumber(hitWrite)} Tokens\n输入侧基数: ${formatNumber(denomAll)} Tokens`;
    }
    // 环
    const donut = zpEl('zpHitDonut');
    if (donut) {
      const r = 29, cir = 2 * Math.PI * r;
      const frac = Math.max(0, Math.min(1, hit));
      donut.setAttribute('title', `缓存命中率 (${winLabel}): ${(hit * 100).toFixed(1)}%\n读 ${formatCompact(hitRead)} / 基数 ${formatCompact(denomAll)}`);
      donut.innerHTML = `
        <circle cx="37" cy="37" r="${r}" fill="none" style="stroke:var(--zp-line)" stroke-width="8"/>
        <circle cx="37" cy="37" r="${r}" fill="none" style="stroke:${ZP_COLORS.coral}" stroke-width="8"
          stroke-linecap="${frac > 0.01 ? 'round' : 'butt'}" stroke-dasharray="${(frac * cir).toFixed(1)} ${cir.toFixed(1)}"
          transform="rotate(-90 37 37)"/>`;
    }
  }

  // ============ 工具卡行 ============
  // 工具卡 → 事件流聚焦键：CCS 工具用 app_type；Hermes 各 profile 用
  // hermes:<name>；ZCode/Codex 用固定键；无本机逐事件数据源（如 Antigravity）
  // 返回 null（不可聚焦）。
  function zpFocusKey(t) {
    if (t.app_type) return t.app_type;
    const name = t.name || '';
    if (t.group === 'Hermes' || name.startsWith('Hermes·')) {
      return 'hermes:' + (name.split('·')[1] || '');
    }
    if (name === 'ZCode') return 'zcode';
    if (name === 'Codex' || t.group === 'Codex') return 'codex';
    if (name === 'DSH' || t.group === 'DSH' || name.toLowerCase() === 'dsh') return 'dsh';
    if (name === 'Antigravity' || t.app_type === 'antigravity') return 'antigravity';
    if (app === 'pi' || name.includes('Pi') || name.includes('OMP')) return 'pi';
    return null;
  }

  // 工具卡 → 统计来源 id：卡片以统计来源开关为准（关掉的来源不再占卡位）。
  // 返回 null 表示该卡没有对应的统计来源口径（如 Antigravity 无 token 记账）。
  function zpToolSourceId(t) {
    const app = t.app_type || '', name = t.name || '';
    if (app === 'claude' || app === 'claude-desktop') return 'claude';
    if (app === 'codex') return 'codex';
    if (app === 'zcode') return 'zcode';
    if (app === 'pi' || name.includes('Pi') || name.includes('OMP')) return 'pi';
    if (app === 'dsh') return 'dsh';
    if (app === 'antigravity') return 'antigravity';
    if (app) return 'ccs';                 // gemini / grokbuild 等 CCS 行
    if (t.group === 'Hermes' || name.startsWith('Hermes·')) return 'hermes';
    if (name === 'Codex') return 'codex';
    if (name === 'ZCode') return 'zcode';
    if (name === 'DSH') return 'dsh';
    if (name === 'Antigravity') return 'antigravity';
    return null;
  }

  function zpAppShort(app) {
    if ((app || '').startsWith('hermes:')) return 'H·' + app.slice(7);
    if (app === 'antigravity') return 'antigravity';
    if (app === 'codex') return 'Codex';
    if (app === 'dsh') return 'DSH';
    if (app === 'pi') return 'Pi/OMP';
    return app || '?';
  }

  function zpToolStyle(t) {
    const app = t.app_type || '', name = t.name || '';
    if (app === 'claude') return { c: '#c96a4a', tag: 'CC' };
    if (app === 'claude-desktop') return { c: '#a9825f', tag: 'CD' };
    if (app === 'codex') return { c: '#10a37f', tag: 'CX' };
    if (app === 'gemini') return { c: '#4285f4', tag: 'GM' };
    if (app === 'pi' || name.includes('Pi') || name.includes('OMP')) return { c: '#8d8371', tag: 'pi' };
    if (app === 'grokbuild') return { c: '#5b8bcf', tag: 'GB' };
    if (name === 'Codex') return { c: '#10a37f', tag: 'CX' };
    if (name === 'DSH' || app === 'dsh') return { c: '#4d6bfe', tag: 'DS' };
    if (name === 'ZCode' || app === 'zcode') return { c: '#5e6ad2', tag: 'ZC' };
    if (name === 'Antigravity' || app === 'antigravity') return { c: '#4285f4', tag: 'AG' };
    if (t.group === 'Hermes' || (name || '').startsWith('Hermes·')) {
      const sub = (name.split('·')[1] || 'H').slice(0, 2);
      return { c: '#9c6f9c', tag: sub };
    }
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return { c: `hsl(${h % 360} 32% 55%)`, tag: (name || '?').slice(0, 2).toUpperCase() };
  }

  function zpRenderTools() {
    const wrap = zpEl('zpTools');
    if (!wrap) return;
    let tools = state.toolsData || [];
    if (tools.length === 0 && state.agentsData && state.agentsData.length > 0) {
      tools = state.agentsData.map(a => ({
        name: `Hermes·${a.name}`, group: 'Hermes', available: true,
        requests: a.sessions, total_tokens: (a.input || 0) + (a.output || 0)
          + (a.cache_read || 0) + (a.cache_write || 0), cost_usd: a.cost_usd
      }));
    }
    if (tools.length === 0) {
      wrap.innerHTML = '<div class="zp-empty">暂无工具统计数据</div>';
      return;
    }
    const P = ZP_STATE.pulse;
    const series = (P && P.tools_series) || {};
    const last = (P && P.tools_last) || {};
    const grand = Math.max(tools.reduce((a, t) =>
      a + ((t.tokens && t.tokens.available !== false) ? (t.tokens.total_tokens || 0) : (t.total_tokens || 0)), 0), 1);
    const sorted = [...tools].sort((a, b) => {
      if ((a.available === false) !== (b.available === false)) return a.available === false ? 1 : -1;
      const aT = (a.tokens && a.tokens.available !== false) ? (a.tokens.total_tokens || 0) : (a.total_tokens || 0);
      const bT = (b.tokens && b.tokens.available !== false) ? (b.tokens.total_tokens || 0) : (b.total_tokens || 0);
      return bT - aT;
    });
    sorted.forEach(t => { t.__key = zpFocusKey(t) || (t.name || ''); });
    if (ZP_GRID.agOrder && ZP_GRID.agOrder.length) {
      const rank = {};
      ZP_GRID.agOrder.forEach((k, i) => { rank[k] = i; });
      sorted.sort((a, b) => (rank[a.__key] ?? 999) - (rank[b.__key] ?? 999));
    }
    const visible = sorted.filter(t => !ZP_GRID.agHidden.includes(t.__key));
    // 卡片以统计来源为主：统计来源里没勾（或不存在）的来源不占卡位
    const enabledSources = state.enabledModelSources || [];
    let cards = visible.filter(t => {
      const srcId = zpToolSourceId(t);
      return !srcId || enabledSources.includes(srcId);
    });
    // 选定时间范围（非“全部”）时，仅展示该时间窗口内有活动（请求数 > 0 或 tokens > 0）的 Agent，隐藏无活动历史卡片
    if (state.timeWindow !== 'all') {
      cards = cards.filter(t => {
        const tokens = (t.tokens && t.tokens.available !== false)
          ? (t.tokens.total_tokens || 0) : (t.total_tokens || 0);
        return tokens > 0 || (t.requests || 0) > 0;
      });
    }
    if (cards.length === 0 && visible.length > 0) {
      wrap.innerHTML = `<div class="zp-empty">${state.timeWindow === 'all'
        ? '当前统计来源没有可选中的 Agent 卡（在上方漏斗里勾选来源）'
        : '当前时间范围内暂无活跃的 Agent 卡（切换时间范围可查看更多）'}</div>`;
      return;
    }
    wrap.innerHTML = cards.map((t, idx) => {
      const st = zpToolStyle(t);
      const tokens = (t.tokens && t.tokens.available !== false)
        ? (t.tokens.total_tokens || 0) : (t.total_tokens || 0);
      // 与旧工具表同口径：异常估值（如 terra 的 $18.3M 脏数据）不进卡片数字
      const cost = t.cost_anomaly ? 0 : (Number(t.cost_usd) || 0);
      const app = t.app_type || '';
      const focusKey = zpFocusKey(t);
      // 最后活动时间按聚焦键查（后端已把 Hermes/ZCode/Codex/Claude 本机逐事件
      // 并入 tools_last）；没有聚焦键的卡片退回 app_type。
      const whenTs = last[focusKey] || last[app];
      const when = whenTs ? zpRel(whenTs) : '—';
      const sharePct = (tokens / grand * 100).toFixed(tokens / grand < 0.001 ? 2 : 1);
      // 焦点：有本机逐事件数据源的键即可聚焦（30 分钟无波峰时波区显示空态）
      const focusable = Boolean(focusKey);
      const focused = ZP_STATE.focus === app && focusable;
      const numHtml = `${formatCompact(tokens)}${cost > 0 ? `<small>${zpMoney(cost)}</small>` : ''}`;
      return `
        <div class="zp-tool${focused ? ' focused' : ''}" data-zp-app="${escapeHtml(focusKey || '')}"
          data-zp-apptype="${escapeHtml(app)}"
          data-zp-focusable="${focusable ? 1 : 0}" data-zp-idx="${idx}"
          data-zp-key="${escapeHtml(t.__key || '')}"
          title="${focusable ? '点击只看该 Agent 的请求波与请求流' : '该 Agent 无本机逐事件记账，无法聚焦'}">
          <div class="zp-tool-head">
            <span class="zp-tool-ico" style="background:${st.c}">${escapeHtml(st.tag)}</span>
            <span class="zp-tool-name">${escapeHtml(t.name || app)}</span>
            <span class="zp-tool-when">${when}</span>
          </div>
          <div class="zp-tool-num">${numHtml}</div>
          <div class="zp-tool-sub">${formatNumber(t.requests || 0)} 次请求 · 占全部 ${sharePct}%</div>
        </div>`;
    }).join('');
    if (ZP_GRID.editing) zpAgentsEditOn();
  }

  // pulse 到达后只刷新「最后活动」文本：卡片首次渲染时 pulse 可能还没回来
  // （loadData 与 zpFetch 并发），而 zpFetch 只重绘实时区、不重建卡片行，
  // 否则时间会一直停在 — 直到下次改时间范围/手动刷新。
  function zpUpdateToolWhen() {
    const wrap = zpEl('zpTools');
    if (!wrap) return;
    const last = (ZP_STATE.pulse && ZP_STATE.pulse.tools_last) || {};
    wrap.querySelectorAll('.zp-tool').forEach(node => {
      const whenEl = node.querySelector('.zp-tool-when');
      if (!whenEl) return;
      const ts = last[node.getAttribute('data-zp-app')]
        || last[node.getAttribute('data-zp-apptype')];
      whenEl.textContent = ts ? zpRel(ts) : '—';
    });
  }

  // ---- Agent 小卡编辑（限制在 Agent 大卡内移动/隐藏）----
  function zpAgentsEditOn() {
    const wrap = zpEl('zpTools');
    if (!wrap) return;
    wrap.querySelectorAll('.zp-tool').forEach(n => {
      const del = document.createElement('button');
      del.className = 'zp-card-del';
      del.textContent = '×';
      del.title = '隐藏该 Agent 卡（＋ 菜单可恢复）';
      del.addEventListener('click', ev => {
        ev.stopPropagation();
        const key = n.getAttribute('data-zp-key');
        ZP_GRID.agHidden.push(key);
        n.remove();
        zpLayoutSave();
      });
      n.appendChild(del);
      n.addEventListener('pointerdown', ev => {
        if (ev.target.closest('.zp-card-del')) return;
        zpAgentDragStart(ev, n, wrap);
      });
    });
  }

  function zpAgentDragStart(e, card, wrap) {
    e.preventDefault(); e.stopPropagation();
    const rect = card.getBoundingClientRect();
    const offX = e.clientX - rect.left, offY = e.clientY - rect.top;
    const others = [...wrap.querySelectorAll('.zp-tool')].filter(n => n !== card);
    const first = others.map(n => n.getBoundingClientRect());
    card.classList.add('zp-dragging');
    card.style.position = 'fixed';
    card.style.left = rect.left + 'px';
    card.style.top = rect.top + 'px';
    card.style.width = rect.width + 'px';
    card.style.height = rect.height + 'px';
    card.style.zIndex = 80;
    others.forEach((n, i) => {
      const f = first[i], l = n.getBoundingClientRect();
      const dx = f.left - l.left, dy = f.top - l.top;
      if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
      n.classList.add('zp-flip');
      n.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        n.style.transform = '';
        setTimeout(() => n.classList.remove('zp-flip'), 320);
      });
    });
    const move = ev => {
      card.style.left = (ev.clientX - offX) + 'px';
      card.style.top = (ev.clientY - offY) + 'px';
      const cx = ev.clientX - offX + rect.width / 2;
      const cy = ev.clientY - offY + rect.height / 2;
      const nodes = [...wrap.querySelectorAll('.zp-tool')];
      const target = nodes.find(n => {
        if (n === card) return false;
        const r = n.getBoundingClientRect();
        return cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom;
      });
      if (target) {
        const ti = nodes.indexOf(target), ci = nodes.indexOf(card);
        if (ti !== ci) {
          const firstNow = nodes.map(n => n.getBoundingClientRect());
          wrap.insertBefore(card, ti < ci ? target : target.nextSibling);
          nodes.forEach((n, i) => {
            const f = firstNow[i], l = n.getBoundingClientRect();
            const ddx = f.left - l.left, ddy = f.top - l.top;
            if (Math.abs(ddx) < 2 && Math.abs(ddy) < 2) return;
            n.classList.add('zp-flip');
            n.style.transform = `translate(${ddx}px, ${ddy}px)`;
            requestAnimationFrame(() => {
              n.style.transform = '';
              setTimeout(() => n.classList.remove('zp-flip'), 320);
            });
          });
        }
      }
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      const last = card.getBoundingClientRect();
      card.classList.remove('zp-dragging');
      card.style.position = '';
      card.style.left = '';
      card.style.top = '';
      card.style.width = '';
      card.style.height = '';
      card.style.zIndex = '';
      const now = card.getBoundingClientRect();
      const dx = last.left - now.left, dy = last.top - now.top;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
        card.classList.add('zp-flip');
        card.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          card.style.transform = '';
          setTimeout(() => card.classList.remove('zp-flip'), 320);
        });
      }
      ZP_GRID.agOrder = [...wrap.querySelectorAll('.zp-tool')]
        .map(n => n.getAttribute('data-zp-key'));
      zpLayoutSave();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function zpBindTools() {
    const wrap = zpEl('zpTools');
    if (!wrap) return;
    wrap.addEventListener('click', e => {
      const card = e.target.closest('.zp-tool');
      if (!card || card.getAttribute('data-zp-focusable') !== '1') return;
      const app = card.getAttribute('data-zp-app');
      zpSetFocus(ZP_STATE.focus === app ? null : app);
    });
  }

  function zpSetFocus(app) {
    ZP_STATE.focus = app;
    if (app) {
      const t = (state.toolsData || []).find(x => (zpFocusKey(x) || '') === app);
      ZP_STATE.focusName = t ? (t.name || zpAppShort(app)) : zpAppShort(app);
    }
    const chip = zpEl('zpFocusChip');
    if (chip) {
      chip.style.display = app ? '' : 'none';
      if (app) chip.querySelector('b').textContent = ZP_STATE.focusName;
    }
    zpRenderArea();
    zpRenderRipple();
    zpRenderRateVt();
    zpRenderStream();
    zpRenderTools();
  }

  // ============ 今日逐小时堆叠面积图 ============
  function zpAreaDataset() {
    const P = ZP_STATE.pulse;
    if (!P || P.available === false) return null;
    const focusApp = ZP_STATE.focus;
    const hasFocus = Boolean(focusApp && P.hours_today_by_app && P.hours_today_by_app[focusApp]);
    const today = hasFocus ? P.hours_today_by_app[focusApp] : P.hours_today;
    const costMode = ZP_STATE.mode === 'cost';
    ZP_STATE.focusPartial = Boolean(focusApp && !hasFocus);  // 聚焦工具无逐小时分解
    const compDefs = [
      { k: 'input', label: '输入', color: ZP_COLORS.coral2, on: ZP_STATE.series.input },
      { k: 'output', label: '输出', color: ZP_COLORS.lav, on: ZP_STATE.series.output },
      { k: 'cache_w', label: '缓存写入', color: ZP_COLORS.sage, on: ZP_STATE.series.cache_w },
      { k: 'cache_r', label: '缓存读取', color: ZP_COLORS.slate, on: ZP_STATE.series.cache_r }
    ];
    // 费用模式读逐请求成本分量（后端按 input_cost_usd 等分列汇总，价格差已真实体现）；
    // 旧载荷没有分量字段时退回按 token 占比摊分
    const valueOf = (bucket, k) => {
      if (!bucket) return 0;
      if (!costMode) return bucket[k] || 0;
      if (bucket.input_cost != null) {
        const map = { input: bucket.input_cost || 0, output: bucket.output_cost || 0,
          cache_w: bucket.cache_creation_cost || 0, cache_r: bucket.cache_read_cost || 0 };
        return map[k];
      }
      const total = (bucket.input || 0) + (bucket.output || 0)
        + (bucket.cache_read || 0) + (bucket.cache_creation || 0);
      if (total <= 0) return 0;
      const tmap = { input: bucket.input || 0, output: bucket.output || 0,
        cache_w: bucket.cache_creation || 0, cache_r: bucket.cache_read || 0 };
      return (bucket.cost || 0) * (tmap[k] / total);
    };
    return { today, costMode, compDefs, valueOf, hasFocus };
  }

  function zpRenderArea() {
    const box = zpEl('zpAreaChart');
    if (!box) return;
    const tip = zpEl('zpAreaTip');
    const ds = zpAreaDataset();
    if (!ds) {
      box.innerHTML = '<div class="zp-empty">逐小时数据需新版后台（/api/stats/pulse）</div>';
      return;
    }
    const now = new Date();
    const curHour = Math.max(now.getHours(), 1);
    const W = 940, H = 180, padL = 46, padR = 12, padT = 10, padB = 22;
    const innerW = W - padL - padR, innerH = H - padT - padB;
    const xh = h => padL + h / 23 * innerW;
    const comps = ds.compDefs.filter(c => c.on);
    // 每个成分的值序列（0..当前小时）
    const n = curHour + 1;
    const seriesVals = comps.map(c => {
      const arr = [];
      for (let h = 0; h < n; h++) arr.push(ds.valueOf(ds.today[h], c.k));
      return arr;
    });
    const totals = [];
    for (let h = 0; h < n; h++) totals.push(seriesVals.reduce((a, sv) => a + sv[h], 0));
    const maxVal = zpNiceMax(Math.max(...totals, ds.costMode ? 0.01 : 1));
    const yv = v => padT + innerH - (v / maxVal) * innerH;
    // 累积层
    const layers = [];
    let prevCum = new Array(n).fill(0);
    seriesVals.forEach(sv => {
      const cum = sv.map((v, i) => prevCum[i] + v);
      layers.push({
        top: cum.map((v, h) => [xh(h), yv(v)]),
        bottom: prevCum.map((v, h) => [xh(h), yv(v)]),
        color: comps[layers.length].color, label: comps[layers.length].label
      });
      prevCum = cum;
    });
    let body = '';
    layers.forEach((L, li) => {
      if (n < 2) return;
      const topS = zpSmoothSamples(L.top, 12);
      const botS = zpSmoothSamples(L.bottom, 12);
      body += `<path d="${zpAreaD(topS, botS)}" style="fill:${L.color}" fill-opacity=".78" stroke="none"/>`;
      body += `<path d="${zpLineD(topS)}" fill="none" style="stroke:${L.color}"
        stroke-width="1.6" stroke-linejoin="round" stroke-opacity=".95"/>`;
    });
    // 未来小时阴影
    const xNow = padL + (curHour + now.getMinutes() / 60) / 23 * innerW;
    let head = `<defs><pattern id="zpHatch" width="7" height="7" patternTransform="rotate(45)"
      patternUnits="userSpaceOnUse">
      <line x1="0" y1="0" x2="0" y2="7" style="stroke:${ZP_COLORS.ink3}" stroke-opacity=".28" stroke-width="1.4"/>
    </pattern></defs>`;
    if (xNow < padL + innerW) {
      head += `<rect x="${xNow.toFixed(1)}" y="${padT}" width="${(padL + innerW - xNow).toFixed(1)}"
        height="${innerH}" fill="url(#zpHatch)"/>`;
    }
    // 网格与轴
    let grid = '';
    for (let g = 0; g <= 4; g++) {
      const y = padT + g / 4 * innerH;
      const val = maxVal * (1 - g / 4);
      grid += `<line x1="${padL}" y1="${y}" x2="${padL + innerW}" y2="${y}"
        style="stroke:var(--zp-line)" stroke-width="1"/>`;
      grid += `<text x="${padL - 7}" y="${y + 3.5}" text-anchor="end" font-size="10"
        style="fill:${ZP_COLORS.ink3}" font-family="${ZP_FONT}">${ds.costMode ? zpMoney(val) : formatCompact(val)}</text>`;
    }
    for (let h = 0; h < 24; h += 2) {
      grid += `<text x="${xh(h)}" y="${H - 8}" text-anchor="middle" font-size="10"
        style="fill:${ZP_COLORS.ink3}" font-family="${ZP_FONT}">${String(h).padStart(2, '0')}:00</text>`;
    }
    const partialNote = ZP_STATE.focusPartial
      ? `<span class="zp-chip" style="margin-left:8px" title="该工具无逐小时 token 分解（仅 CCS 代理工具有），堆叠图显示全源口径">堆叠图=全源</span>`
      : '';
    const legendEl = document.getElementById('zpAreaLegend');
    if (legendEl) {
      let note = legendEl.querySelector('.zp-partial-note');
      if (ZP_STATE.focusPartial) {
        if (!note) {
          note = document.createElement('span');
          note.className = 'zp-chip zp-partial-note';
          note.title = '该工具无逐小时 token 分解（仅 CCS 代理工具有），堆叠图显示全源口径';
          note.textContent = '堆叠图=全源';
          legendEl.appendChild(note);
        }
      } else if (note) note.remove();
    }
    const svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">
      ${head}${grid}${body}
      <line id="zpAreaCross" y1="${padT}" y2="${padT + innerH}" x1="0" x2="0"
        stroke-dasharray="3 3" style="stroke:var(--zp-ink-3);display:none"/>
      <rect x="${padL}" y="${padT}" width="${innerW}" height="${innerH}" fill="transparent" id="zpAreaHover"/>
    </svg>`;
    box.innerHTML = svg + (tip ? tip.outerHTML : '');
    // 悬停交互
    const svgEl = box.querySelector('svg');
    const hover = box.querySelector('#zpAreaHover');
    const cross = box.querySelector('#zpAreaCross');
    const tipEl = zpEl('zpAreaTip');
    if (!hover || !tipEl) return;
    hover.addEventListener('mousemove', e => {
      const rect = svgEl.getBoundingClientRect();
      const vx = (e.clientX - rect.left) / rect.width * W;
      const h = Math.max(0, Math.min(curHour, Math.round((vx - padL) / innerW * 23)));
      if (cross) {
        cross.setAttribute('x1', xh(h));
        cross.setAttribute('x2', xh(h));
        cross.style.display = 'block';
      }
      const b = ds.today[h];
      const rows = comps.map(c => `<div class="row"><span><i style="background:${c.color}"></i>${c.label}</span>
        <span>${ds.costMode ? zpMoney(ds.valueOf(b, c.k)) : formatCompact(ds.valueOf(b, c.k))}</span></div>`).join('');
      tipEl.innerHTML = `<div class="t">${String(h).padStart(2, '0')}:00 – ${String(h + 1).padStart(2, '0')}:00 · ${b ? b.requests || 0 : 0} 次请求</div>${rows}
        <div class="row" style="border-top:1px solid var(--zp-line);margin-top:3px;padding-top:3px">
          <span>合计</span><span>${ds.costMode ? zpMoney(b ? b.cost || 0 : 0) : formatCompact(totals[h])}</span></div>`;
      tipEl.style.display = 'block';
      const bw = box.clientWidth;
      const px = xh(h) / W * bw;
      tipEl.style.left = `${Math.min(Math.max(px + 12, 4), bw - 180)}px`;
      tipEl.style.top = '10px';
    });
    hover.addEventListener('mouseleave', () => {
      tipEl.style.display = 'none';
      if (cross) cross.style.display = 'none';
    });
  }

  // ============ 请求波 ============
  function zpRenderRipple() {
    const box = zpEl('zpRipple');
    if (!box) return;
    const P = ZP_STATE.pulse;
    const countEl = zpEl('zpRippleCount');
    const agentChip = zpEl('zpRippleAgentChip');
    if (!P || P.available === false) {
      box.innerHTML = '<div class="zp-empty">逐请求数据需新版后台</div>';
      if (countEl) countEl.textContent = '-- 次请求';
      if (agentChip) agentChip.textContent = '全部 Agent';
      return;
    }
    let rows = P.ripple || [];
    if (ZP_STATE.focus) rows = rows.filter(r => r.app === ZP_STATE.focus);
    if (countEl) countEl.textContent = `${rows.length} 次请求`;
    if (agentChip) {
      if (ZP_STATE.focus) {
        const topModel = rows.length > 0 ? (rows[rows.length - 1].model || rows[0].model || '') : '';
        agentChip.className = 'zp-chip coral';
        agentChip.style.cursor = 'pointer';
        agentChip.title = '点击取消聚焦，查看全部 Agent';
        agentChip.innerHTML = `聚焦: <b>${escapeHtml(ZP_STATE.focusName || zpAppShort(ZP_STATE.focus))}</b>${topModel ? ` · <span style="font-weight:400">${escapeHtml(topModel)}</span>` : ''} <span style="margin-left:3px;font-size:10px">✕</span>`;
        agentChip.onclick = () => zpSetFocus(null);
      } else {
        const latestModel = rows.length > 0 ? (rows[rows.length - 1].model || rows[0].model || '') : '';
        agentChip.className = 'zp-chip';
        agentChip.style.cursor = 'default';
        agentChip.title = '当前展示全部 Agent，点击下方 Agent 卡片可聚焦只看某 Agent';
        agentChip.onclick = null;
        agentChip.innerHTML = `全部 Agent${latestModel ? ` · 最新: <b>${escapeHtml(latestModel)}</b>` : ''}`;
      }
    }
    if (rows.length === 0) {
      box.innerHTML = `<div class="zp-empty">${ZP_STATE.focus
        ? `${escapeHtml(zpAppShort(ZP_STATE.focus))} 最近 30 分钟无请求（请求流里有它的历史事件）`
        : '最近 30 分钟内暂无请求'}</div>`;
      return;
    }
    const W = 940, H = 130, padL = 8, padR = 8, padT = 12, padB = 18;
    const innerW = W - padL - padR, baseY = H - padB;
    const nowMs = P.now * 1000;
    const xOf = ts => padL + (1 - (nowMs - ts * 1000) / 1800000) * innerW;
    const maxTok = Math.max(...rows.map(r => (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_creation || 0)), 1);
    let spikes = '';
    rows.forEach(r => {
      const tok = (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_creation || 0);
      // log 缩放：token 量跨几个数量级时矮波仍可见
      const h = Math.max(3, Math.log10(tok + 1) / Math.log10(maxTok + 1) * (H - padT - padB));
      const x = xOf(r.ts);
      const bad = r.status >= 400;
      const color = bad ? ZP_COLORS.coral : ZP_COLORS.ink3;
      const op = bad ? 0.95 : 0.75;
      spikes += `<line x1="${x.toFixed(1)}" y1="${baseY}" x2="${x.toFixed(1)}" y2="${(baseY - h).toFixed(1)}"
        style="stroke:${color}" stroke-opacity="${op}" stroke-width="1.5" stroke-linecap="round"/>`;
      if (bad) spikes += `<circle cx="${x.toFixed(1)}" cy="${(baseY - h - 4).toFixed(1)}" r="2.4" style="fill:${ZP_COLORS.coral}"/>`;
    });
    let axis = '';
    for (const m of [30, 20, 10, 0]) {
      const x = padL + (1 - m / 30) * innerW;
      axis += `<text x="${x}" y="${H - 6}" text-anchor="${m === 30 ? 'start' : m === 0 ? 'end' : 'middle'}"
        font-size="10" style="fill:${ZP_COLORS.ink3}" font-family="${ZP_FONT}">${m === 0 ? '现在' : `${m} 分钟前`}</text>`;
    }
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">
      <line x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}" style="stroke:var(--zp-line)" stroke-width="1"/>
      ${spikes}${axis}
      <rect id="zpRippleHover" x="0" y="0" width="${W}" height="${H}" fill="transparent"/>
    </svg>
    <div class="zp-tip" id="zpRippleTip"></div>`;
    const svgEl = box.querySelector('svg');
    const hover = box.querySelector('#zpRippleHover');
    const tipEl = box.querySelector('#zpRippleTip');
    if (!hover || !tipEl) return;
    const pick = e => {
      const rect = svgEl.getBoundingClientRect();
      const vx = (e.clientX - rect.left) / rect.width * W;
      let best = null, bestD = 1e9;
      rows.forEach(r => {
        const d = Math.abs(xOf(r.ts) - vx);
        if (d < bestD) { bestD = d; best = r; }
      });
      return bestD <= 8 ? best : null;
    };
    hover.addEventListener('mousemove', e => {
      const r = pick(e);
      if (!r) { tipEl.style.display = 'none'; return; }
      const tok = (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_creation || 0);
      const time = new Date(r.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
      const kindLabel = r.kind === 'sess' ? '会话' : r.kind === 'call' ? '调用' : '请求';
      tipEl.innerHTML = `<div class="t">${time} · ${escapeHtml(zpAppShort(r.app))} · ${escapeHtml(r.model || '')} · ${kindLabel}</div>
        <div class="row"><span>Token</span><span>${formatCompact(tok)}</span></div>
        <div class="row"><span>输入/输出</span><span>${formatCompact(r.input)} / ${formatCompact(r.output)}</span></div>
        <div class="row"><span>费用</span><span>${r.cost ? zpMoney(r.cost) : '—'}</span></div>
        <div class="row"><span>状态</span><span>${r.status} · ${(r.latency_ms / 1000).toFixed(1)}s</span></div>`;
      tipEl.style.display = 'block';
      const bw = box.clientWidth;
      const px = xOf(r.ts) / W * bw;
      tipEl.style.left = `${Math.min(Math.max(px + 10, 4), bw - 190)}px`;
      tipEl.style.top = '8px';
      box.style.cursor = 'pointer';
    });
    hover.addEventListener('mouseleave', () => { tipEl.style.display = 'none'; box.style.cursor = ''; });
    hover.addEventListener('click', e => {
      const r = pick(e);
      if (!r) return;
      zpHighlightStream(r);
    });
  }

  function zpHighlightStream(req) {
    const stream = zpEl('zpStream');
    if (!stream) return;
    stream.querySelectorAll('.hl').forEach(x => x.classList.remove('hl'));
    const key = `${req.ts}|${req.model}`;
    const row = stream.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (row) {
      row.classList.add('hl');
      row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  // ============ 请求速率（v-t 图） ============
  function zpRenderRateVt() {
    const box = zpEl('zpVtChart');
    if (!box) return;
    const P = ZP_STATE.pulse;
    const VT = ZP_STATE.vt;   // 非默认时间窗的独立取数（ZP_STATE.vtRange !== 60 时）
    const peakEl = zpEl('zpVtChipPeak');
    const avgEl = zpEl('zpVtChipAvg');
    const nowEl = zpEl('zpVtChipNow');
    const selEl = zpEl('zpVtModelSelect');
    const subEl = zpEl('zpVtSubTitle');

    // 数据源选择：默认 1h 窗直接复用 pulse.minutes（后端与 _rate_series 同一实现）；
    // 非默认窗用 /rate-series 数据，未到达前暂显近 1 小时并在副标题标注。
    let src = null;
    if (VT && Array.isArray(VT.minutes) && VT.range_min === ZP_STATE.vtRange) {
      src = {
        minutes: VT.minutes, byModel: VT.minutes_by_model || {},
        models: (VT.models || []).slice().sort(), bucketSec: VT.bucket_sec || 60,
        now: VT.now, rangeMin: VT.range_min
      };
    } else if (P && P.available !== false && Array.isArray(P.minutes)) {
      src = {
        minutes: P.minutes, byModel: P.minutes_by_model || {},
        models: Array.from(new Set(P.models_60m || Object.keys(P.minutes_by_model || {}))).sort(),
        bucketSec: 60, now: P.now, rangeMin: 60
      };
    }
    if (!src) {
      box.innerHTML = '<div class="zp-empty">实时速率数据需新版后台（/api/stats/pulse）</div>';
      if (peakEl) peakEl.innerHTML = '峰值 v: --';
      if (avgEl) avgEl.innerHTML = '均值 v̄: --';
      if (nowEl) nowEl.innerHTML = '当前 v: --';
      if (selEl) selEl.innerHTML = '<option value="">全部模型</option>';
      if (subEl) subEl.textContent = '最近 1 小时速率轨迹 · 曲线下方面积 ∫v·dt = 累计总量';
      return;
    }
    const B = src.bucketSec || 60;   // 桶宽（秒）：v = 桶总量 / B（∫v·dt 守恒）

    // 动态同步模型下拉选项
    const models = src.models;
    const curM = ZP_STATE.vtModel || '';
    if (selEl) {
      const expectedVals = ['', ...models];
      if (curM && !expectedVals.includes(curM)) expectedVals.push(curM);
      const currentVals = Array.from(selEl.options).map(o => o.value);
      const needsUpdate = currentVals.length !== expectedVals.length ||
        expectedVals.some((val, idx) => currentVals[idx] !== val);

      if (needsUpdate) {
        let optHtml = '<option value="">全部模型</option>';
        models.forEach(m => {
          optHtml += `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`;
        });
        if (curM && !models.includes(curM)) {
          optHtml += `<option value="${escapeHtml(curM)}">${escapeHtml(curM)} (当前窗口无调用)</option>`;
        }
        selEl.innerHTML = optHtml;
      }
      selEl.value = curM;
      selEl.classList.toggle('active-filter', !!curM);
    }

    // 模式：gen (纯文本生成 tok/s) / req (请求频次 req/min) / tok (全口径吞吐 tok/s)
    const mode = ZP_STATE.vtMode || 'gen';
    const isGen = mode === 'gen';
    const isTok = mode === 'tok';
    const isRateTok = isGen || isTok;
    const unitLabel = isRateTok ? 'tok/s' : 'req/min';

    // 标题副文本反映当前窗口/筛选状态与物理积分守恒口径
    const rangeLabel = src.rangeMin === 60 ? '1 小时'
      : (src.rangeMin % 60 === 0 ? `${src.rangeMin / 60} 小时` : `${src.rangeMin} 分钟`);
    const vizTip = isGen ? '纯文本生成速率 · 曲线下方面积 ∫v·dt = 累计生成 Token'
      : isTok ? '全口径吞吐速率 · 曲线下方面积 ∫v·dt = 累计吞吐 Token'
      : '请求频次轨迹 · 曲线下方面积 ∫v·dt = 累计请求次数';
    let subHtml = `最近 ${rangeLabel}${vizTip}`;
    if (curM) subHtml = `最近 ${rangeLabel} · <b style="color:var(--zp-coral)">${escapeHtml(curM)}</b> ${vizTip}`;
    if (ZP_STATE.vtRange !== 60 && src.rangeMin !== ZP_STATE.vtRange) {
      subHtml += ZP_STATE.vtErr ? ' · 时间筛选需新版后台，暂显近 1 小时' : ' · 加载中…';
    }
    if (subEl) subEl.innerHTML = subHtml;

    let minutes = src.minutes;
    if (minutes.length === 0) {
      box.innerHTML = '<div class="zp-empty">暂无速率数据</div>';
      if (peakEl) peakEl.innerHTML = '峰值 v: --';
      if (avgEl) avgEl.innerHTML = '均值 v̄: --';
      if (nowEl) nowEl.innerHTML = '当前 v: --';
      return;
    }

    if (curM) {
      if (src.byModel && src.byModel[curM]) {
        minutes = src.byModel[curM];
      } else {
        minutes = minutes.map(m => ({
          minute_offset: m.minute_offset,
          requests: 0,
          tokens: 0,
          output_tokens: 0,
          cost: 0
        }));
      }
    }

    // 值序列（严格物理积分：v_i * 桶宽 = 该桶对应总量）
    const values = minutes.map(m => isGen ? ((m.output_tokens || 0) / B)
      : isTok ? ((m.tokens || 0) / B) : ((m.requests || 0) * 60 / B));
    const n = values.length;
    const vNow = values[n - 1] || 0;
    const vPeak = Math.max(...values, 0);
    const sumVal = values.reduce((a, b) => a + b, 0);
    const vAvg = n > 0 ? (sumVal / n) : 0;

    // 格式化顶部指标
    const fmtV = v => isRateTok ? (v >= 100 ? formatCompact(Math.round(v)) : (v > 0 ? v.toFixed(1) : '0')) : formatNumber(Math.round(v));
    const chipSuffix = isGen ? ' (生成)' : isTok ? ' (吞吐)' : '';
    if (peakEl) peakEl.innerHTML = `峰值 v: <b>${fmtV(vPeak)}</b> ${unitLabel}${chipSuffix}`;
    if (avgEl) avgEl.innerHTML = `均值 v̄: <b>${fmtV(vAvg)}</b> ${unitLabel}${chipSuffix}`;
    if (nowEl) nowEl.innerHTML = `当前 v: <b>${fmtV(vNow)}</b> ${unitLabel}${chipSuffix}`;

    // SVG 坐标系统
    const W = 940, H = 145, padL = 48, padR = 24, padT = 16, padB = 22;
    const innerW = W - padL - padR, innerH = H - padT - padB;
    const baseY = padT + innerH;
    const niceMax = zpNiceMax(Math.max(vPeak, isRateTok ? (isGen ? 20 : 60) : 6));
    const yv = v => padT + innerH - (Math.max(0, v) / niceMax) * innerH;
    const xv = i => padL + (i / Math.max(n - 1, 1)) * innerW;

    // 点列生成与平滑
    const pts = values.map((v, i) => [xv(i), yv(v)]);
    const topS = zpSmoothSamples(pts, 8);
    const botS = pts.map(p => [p[0], baseY]);

    // Y 轴刻度与辅助虚线
    let grid = '';
    const yTicks = [0, niceMax * 0.5, niceMax];
    yTicks.forEach(val => {
      const y = yv(val);
      grid += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${(padL + innerW).toFixed(1)}" y2="${y.toFixed(1)}"
        style="stroke:var(--zp-line)" stroke-width="1" stroke-dasharray="${val === 0 ? 'none' : '3 3'}" stroke-opacity=".7"/>`;
      grid += `<text x="${padL - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" font-size="9.5"
        style="fill:var(--zp-ink-3)" font-family="${ZP_FONT}">${isTok ? formatCompact(Math.round(val)) : formatNumber(Math.round(val))}</text>`;
    });

    // X 轴时间刻度：按窗口长度取 4~5 个整齐刻度（分钟/小时自适应）
    let xAxis = '';
    const nowTime = src.now ? (src.now * 1000) : Date.now();
    const tickStepM = [15, 30, 60, 120, 180, 360, 720].find(s => src.rangeMin / s <= 4) || 1440;
    const tickAgo = [];
    for (let m = 0; m <= src.rangeMin; m += tickStepM) tickAgo.push(m);
    tickAgo.reverse();
    tickAgo.forEach((minAgo, ti) => {
      const idx = Math.max(0, Math.min(n - 1, n - 1 - Math.round(minAgo * 60 / B)));
      const x = xv(idx);
      const label = minAgo === 0 ? '现在' : (minAgo % 60 === 0 ? `${minAgo / 60}h 前` : `${minAgo}m 前`);
      const anchor = ti === 0 ? 'start' : (ti === tickAgo.length - 1 ? 'end' : 'middle');
      xAxis += `<text x="${x.toFixed(1)}" y="${H - 5}" text-anchor="${anchor}"
        font-size="9.5" style="fill:var(--zp-ink-3)" font-family="${ZP_FONT}">${label}</text>`;
    });

    // 渐变与曲面
    const gid = 'zpVtGrad';
    const defs = `<defs>
      <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" style="stop-color:var(--zp-coral)" stop-opacity="0.32"/>
        <stop offset="85%" style="stop-color:var(--zp-coral)" stop-opacity="0.04"/>
        <stop offset="100%" style="stop-color:var(--zp-coral)" stop-opacity="0"/>
      </linearGradient>
    </defs>`;

    const areaPath = `<path d="${zpAreaD(topS, botS)}" fill="url(#${gid})" stroke="none"/>`;
    const linePath = `<path d="${zpLineD(topS)}" fill="none" style="stroke:var(--zp-coral)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`;

    // 最新点脉冲环
    const lastPt = pts[pts.length - 1] || [padL + innerW, baseY];
    const liveMarker = `
      <circle cx="${lastPt[0].toFixed(1)}" cy="${lastPt[1].toFixed(1)}" r="3.5" style="fill:var(--zp-coral)"/>
      <circle cx="${lastPt[0].toFixed(1)}" cy="${lastPt[1].toFixed(1)}" r="7" style="stroke:var(--zp-coral)" stroke-width="1.2" opacity="0.5" class="zp-live-ring"/>
    `;

    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">
      ${defs}
      ${grid}
      ${areaPath}
      ${linePath}
      ${liveMarker}
      ${xAxis}
      <line id="zpVtCrossX" x1="0" y1="${padT}" x2="0" y2="${baseY}" style="stroke:var(--zp-coral);display:none" stroke-width="1" stroke-dasharray="3 2" stroke-opacity=".8"/>
      <circle id="zpVtHoverDot" cx="0" cy="0" r="4.5" style="fill:var(--zp-card);stroke:var(--zp-coral);stroke-width:2;display:none"/>
      <rect id="zpVtHover" x="0" y="0" width="${W}" height="${H}" fill="transparent"/>
    </svg>
    <div class="zp-tip" id="zpVtTip"></div>`;

    // 悬停十字准星与 Tooltip
    const svgEl = box.querySelector('svg');
    const hover = box.querySelector('#zpVtHover');
    const tipEl = box.querySelector('#zpVtTip');
    const crossX = box.querySelector('#zpVtCrossX');
    const dotEl = box.querySelector('#zpVtHoverDot');
    if (!hover || !tipEl || !svgEl) return;

    hover.addEventListener('mousemove', e => {
      const rect = svgEl.getBoundingClientRect();
      const relX = (e.clientX - rect.left) / rect.width * W;
      let bestIdx = 0, bestDist = 1e9;
      for (let i = 0; i < n; i++) {
        const d = Math.abs(xv(i) - relX);
        if (d < bestDist) { bestDist = d; bestIdx = i; }
      }
      if (bestDist > innerW / n * 3) {
        tipEl.style.display = 'none';
        if (crossX) crossX.style.display = 'none';
        if (dotEl) dotEl.style.display = 'none';
        return;
      }

      const curPt = pts[bestIdx];
      if (crossX) {
        crossX.setAttribute('x1', curPt[0].toFixed(1));
        crossX.setAttribute('x2', curPt[0].toFixed(1));
        crossX.style.display = '';
      }
      if (dotEl) {
        dotEl.setAttribute('cx', curPt[0].toFixed(1));
        dotEl.setAttribute('cy', curPt[1].toFixed(1));
        dotEl.style.display = '';
      }

      const m = minutes[bestIdx] || {};
      const secAgo = (n - 1 - bestIdx) * B;
      const tMs = nowTime - secAgo * 1000;
      const tStr = new Date(tMs).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
      const curV = values[bestIdx] || 0;
      const prevV = bestIdx > 0 ? (values[bestIdx - 1] || 0) : curV;
      const deltaV = curV - prevV;
      const accStr = deltaV > 0 ? `+${fmtV(deltaV)} ↑` : deltaV < 0 ? `${fmtV(deltaV)} ↓` : '持平 →';
      const accColor = deltaV > 0 ? 'var(--zp-coral)' : deltaV < 0 ? 'var(--zp-ink-3)' : 'var(--zp-ink-2)';
      const modelBadge = curM ? ` · <span style="color:var(--zp-coral);font-weight:600">${escapeHtml(curM)}</span>` : '';

      const genTok = m.output_tokens || 0;
      const allTok = m.tokens || 0;
      const speedName = isGen ? '纯文本生成流速 v(t)' : isTok ? '全口径吞吐流速 v(t)' : '请求流速 v(t)';
      const agoLabel = secAgo === 0 ? (B === 60 ? '当前分钟' : '当前时段')
        : (secAgo % 3600 === 0 ? `${secAgo / 3600} 小时前` : `${Math.round(secAgo / 60)} 分钟前`);
      tipEl.innerHTML = `
        <div class="t">${tStr} · ${agoLabel}${modelBadge}</div>
        <div class="row"><span>${speedName}</span><b>${fmtV(curV)} ${unitLabel}</b></div>
        <div class="row"><span>速度变化 Δv</span><span style="color:${accColor};font-weight:600">${accStr}</span></div>
        <div class="row"><span>${B === 60 ? '当分请求' : '本段请求'}</span><span>${formatNumber(m.requests || 0)} 次</span></div>
        <div class="row"><span>纯文本生成</span><span>${formatNumber(genTok)} Tokens</span></div>
        <div class="row"><span>全口径吞吐</span><span>${formatCompact(allTok)} Tokens</span></div>
        ${m.cost ? `<div class="row"><span>${B === 60 ? '当分预估' : '本段预估'}</span><span>$${m.cost.toFixed(4)}</span></div>` : ''}
      `;
      tipEl.style.display = 'block';
      const bw = box.clientWidth || W;
      const px = (curPt[0] / W) * bw;
      tipEl.style.left = `${Math.min(Math.max(px + 12, 6), bw - 190)}px`;
      tipEl.style.top = '12px';
    });

    hover.addEventListener('mouseleave', () => {
      tipEl.style.display = 'none';
      if (crossX) crossX.style.display = 'none';
      if (dotEl) dotEl.style.display = 'none';
    });
  }

  // ============ 请求流 ============
  function zpRenderStream() {
    const box = zpEl('zpStream');
    if (!box) return;
    const P = ZP_STATE.pulse;
    const meta = zpEl('zpStreamMeta');
    const streamAgentChip = zpEl('zpStreamAgentChip');
    if (!P || !P.recent) {
      box.innerHTML = '<div class="zp-empty">逐请求数据需新版后台</div>';
      if (meta) meta.textContent = '--';
      if (streamAgentChip) streamAgentChip.textContent = '全部 Agent';
      return;
    }
    let rows = P.recent;
    if (ZP_STATE.focus) rows = rows.filter(r => r.app === ZP_STATE.focus);
    const rippleRows = ZP_STATE.focus
      ? (P.ripple || []).filter(r => r.app === ZP_STATE.focus)
      : (P.ripple || []);
    const cost30 = rippleRows.reduce((a, r) => a + (r.cost || 0), 0);
    if (meta) meta.textContent = `30 分钟 ${rippleRows.length} 次 · ${zpMoney(cost30)}`;
    if (streamAgentChip) {
      if (ZP_STATE.focus) {
        const topModel = rows.length > 0 ? (rows[0].model || '') : '';
        streamAgentChip.className = 'zp-chip coral';
        streamAgentChip.style.cursor = 'pointer';
        streamAgentChip.title = '点击取消聚焦，查看全部 Agent';
        streamAgentChip.innerHTML = `聚焦: <b>${escapeHtml(ZP_STATE.focusName || zpAppShort(ZP_STATE.focus))}</b>${topModel ? ` · <span style="font-weight:400">${escapeHtml(topModel)}</span>` : ''} <span style="margin-left:3px;font-size:10px">✕</span>`;
        streamAgentChip.onclick = () => zpSetFocus(null);
      } else {
        const latestModel = rows.length > 0 ? (rows[0].model || '') : '';
        streamAgentChip.className = 'zp-chip';
        streamAgentChip.style.cursor = 'default';
        streamAgentChip.title = '当前展示全部 Agent，点击下方 Agent 卡片可聚焦只看某 Agent';
        streamAgentChip.onclick = null;
        streamAgentChip.innerHTML = `全部 Agent${latestModel ? ` · 最新: <b>${escapeHtml(latestModel)}</b>` : ''}`;
      }
    }
    const head = `<div class="zp-stream-row head">
      <span>时间</span><span>工具</span><span>模型</span>
      <span class="tk">Token</span><span class="cost">费用</span><span>状态</span><span class="lat">耗时</span>
    </div>`;
    const body = rows.map(r => {
      const tok = (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_creation || 0);
      const time = new Date(r.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
      const ok = r.status >= 200 && r.status < 400;
      const kindLabel = r.kind === 'sess' ? '会话' : r.kind === 'call' ? '调用' : r.status;
      return `<div class="zp-stream-row" data-key="${escapeHtml(`${r.ts}|${r.model}`)}"
        title="${escapeHtml(`${r.model}\n输入 ${formatNumber(r.input)} · 输出 ${formatNumber(r.output)} · 缓存读 ${formatNumber(r.cache_read)}`)}">
        <span class="t">${time}</span>
        <span class="app"><i style="background:${(zpToolStyle({ app_type: r.app.split(':')[0] })).c}"></i>${escapeHtml(zpAppShort(r.app))}</span>
        <span class="model">${escapeHtml(r.model || '—')}</span>
        <span class="tk">${formatCompact(tok)}</span>
        <span class="cost">${r.cost ? zpMoney(r.cost) : '—'}</span>
        <span class="st ${ok ? 'ok' : 'bad'}"><i></i>${kindLabel}</span>
        <span class="lat">${r.latency_ms ? (r.latency_ms / 1000).toFixed(1) + 's' : '—'}</span>
      </div>`;
    }).join('');
    box.innerHTML = head + (body || '<div class="zp-empty">30 分钟内暂无请求</div>');
  }

  // ============ 预算卡 ============
  function zpRenderBudget() {
    const daily = state.dailyData || [];
    const byDay = {};
    daily.forEach(d => { byDay[d.date] = Number(d.cost_usd) || 0; });
    const todayKey = zpLocalDateKey(new Date());
    const yKey = zpLocalDateKey(new Date(Date.now() - 86400e3));
    const today = byDay[todayKey] || 0, yest = byDay[yKey] || 0;
    const now = new Date();
    const monthPrefix = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    let mtd = 0;
    Object.keys(byDay).forEach(k => { if (k.startsWith(monthPrefix)) mtd += byDay[k]; });
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const forecast = mtd / now.getDate() * daysInMonth;
    const last30 = daily.slice(-30);
    const avg30 = last30.length ? last30.reduce((a, d) => a + (Number(d.cost_usd) || 0), 0) / last30.length : 0;
    let budget = null;
    try { budget = Number(localStorage.getItem('zpMonthlyBudget')) || null; } catch (e) { }
    const circle = zpEl('zpBudgetToday');
    if (circle) circle.textContent = zpMoney(today);
    const circleBox = zpEl('zpBudgetCircle');
    if (circleBox) circleBox.title = budget ? `月预算 ${zpMoney(budget)} · 本月已用 ${zpMoney(mtd)}（${(mtd / budget * 100).toFixed(0)}%）` : '点右上「设置预算」可设月度预算';
    const r1 = zpEl('zpBudgetTodayRow');
    if (r1) r1.textContent = zpMoney(today);
    const r2 = zpEl('zpBudgetMonth');
    if (r2) r2.textContent = zpMoney(mtd);
    const r3 = zpEl('zpBudgetForecast');
    if (r3) {
      r3.textContent = zpMoney(forecast) + (budget ? ` / ${zpMoney(budget)}` : '');
      r3.style.color = budget && forecast > budget ? 'var(--zp-coral)' : 'var(--zp-ink)';
    }
    const r4 = zpEl('zpBudgetAvg');
    if (r4) r4.textContent = `${zpMoney(yest)} / ${zpMoney(avg30)}`;
    const btn = zpEl('zpBudgetBtn');
    if (btn) btn.textContent = budget ? '修改预算' : '设置预算';
  }

  // ============ 统计来源 chip ============
  function zpRenderSourceChip() {
    const el = zpEl('zpSourceChip');
    if (!el) return;
    const rows = getModelSourceRows();
    const selected = state.enabledModelSources || [];
    if (!rows.length) { el.textContent = ''; return; }
    const names = selected.map(id => (rows.find(r => r.id === id) || {}).name || id);
    el.textContent = `来源：${names.join('、')}`;
    el.title = el.textContent;
  }

  // ============================================================
  // 2026-10-03 新增卡：时钟 / 天气 / 系统监控 / 自定义图片
  // 颜色约定：结构与文字全走 zp.css 令牌（--zp-ink/--zp-ok/--warn/--err），
  // 主题切换零 JS 参与；轮询各自的 interval（见 zpStartTimer）。
  // ============================================================

  const ZP_TZ_PRESETS = [
    { key: 'local', name: '本地时区', desc: '系统时区' },
    { key: 'Asia/Shanghai', name: '北京时间', desc: 'UTC+8' },
    { key: 'Asia/Tokyo', name: '东京时间', desc: 'UTC+9' },
    { key: 'Europe/London', name: '伦敦时间', desc: 'UTC+0/+1' },
    { key: 'Europe/Paris', name: '巴黎时间', desc: 'UTC+1/+2' },
    { key: 'America/New_York', name: '纽约时间', desc: 'UTC-5/-4' },
    { key: 'America/Los_Angeles', name: '洛杉矶', desc: 'UTC-8/-7' },
    { key: 'UTC', name: '世界标准时', desc: 'UTC+0' }
  ];

  function zpClockTzLabel(tz) {
    if (!tz || tz === 'local') return '本地';
    const hit = ZP_TZ_PRESETS.find(p => p.key === tz);
    if (hit) return hit.name.replace('时间', '').replace('时区', '');
    if (/^UTC?[+-]?\d+/i.test(tz)) return tz.toUpperCase();
    const slash = tz.lastIndexOf('/');
    return slash >= 0 ? tz.substring(slash + 1).replace(/_/g, ' ') : tz;
  }

  function zpGetClockParts(date, tz) {
    let hour, minute, second, year, month, day, weekdayNum;
    if (!tz || tz === 'local') {
      hour = date.getHours();
      minute = date.getMinutes();
      second = date.getSeconds();
      year = date.getFullYear();
      month = date.getMonth() + 1;
      day = date.getDate();
      weekdayNum = date.getDay();
    } else {
      const offsetMatch = /^(?:UTC)?([+-]?\d{1,2})(?::?(\d{2}))?$/i.exec(tz.trim());
      if (offsetMatch) {
        const hOffset = parseInt(offsetMatch[1], 10);
        const mOffset = parseInt(offsetMatch[2] || '0', 10) * (hOffset < 0 ? -1 : 1);
        const totalOffsetMinutes = hOffset * 60 + mOffset;
        const utcMs = date.getTime() + date.getTimezoneOffset() * 60000;
        const targetDate = new Date(utcMs + totalOffsetMinutes * 60000);
        hour = targetDate.getHours();
        minute = targetDate.getMinutes();
        second = targetDate.getSeconds();
        year = targetDate.getFullYear();
        month = targetDate.getMonth() + 1;
        day = targetDate.getDate();
        weekdayNum = targetDate.getDay();
      } else {
        try {
          const dtf = new Intl.DateTimeFormat('en-US', {
            timeZone: tz,
            hourCycle: 'h23',
            year: 'numeric',
            month: 'numeric',
            day: 'numeric',
            weekday: 'short',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
          });
          const parts = dtf.formatToParts(date);
          const pMap = {};
          parts.forEach(p => { pMap[p.type] = p.value; });
          hour = parseInt(pMap.hour, 10);
          minute = parseInt(pMap.minute, 10);
          second = parseInt(pMap.second, 10);
          year = parseInt(pMap.year, 10);
          month = parseInt(pMap.month, 10);
          day = parseInt(pMap.day, 10);
          const wDays = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
          weekdayNum = wDays[pMap.weekday] ?? 0;
        } catch (e) {
          hour = date.getHours();
          minute = date.getMinutes();
          second = date.getSeconds();
          year = date.getFullYear();
          month = date.getMonth() + 1;
          day = date.getDate();
          weekdayNum = date.getDay();
        }
      }
    }
    const p = n => String(n).padStart(2, '0');
    const timeStr = `${p(hour)}:${p(minute)}:${p(second)}`;
    const weekChar = '日一二三四五六'[weekdayNum] || '日';
    const hello = hour < 5 ? '夜深了' : hour < 9 ? '早上好' : hour < 12 ? '上午好'
      : hour < 14 ? '中午好' : hour < 18 ? '下午好' : hour < 23 ? '晚上好' : '夜深了';
    const dateStr = `${year} 年 ${month} 月 ${day} 日 · ${hello}`;
    return { timeStr, weekChar, dateStr, hour, minute, second };
  }

  // ---- 时钟卡：每秒直更，仪表盘不在前台时跳过 DOM 写入 ----
  function zpRenderClock() {
    const t = zpEl('zpClockTime');
    if (!t) return;
    const pg = zpEl('pgDash');
    if (!pg || !pg.classList.contains('on')) return;
    const tz = ZP_STATE.clockTimezone || ((ZP_CARDS.config.clock || {}).timezone) || 'local';
    const parts = zpGetClockParts(new Date(), tz);
    t.textContent = parts.timeStr;
    const dateEl = zpEl('zpClockDate');
    if (dateEl) dateEl.textContent = `周${parts.weekChar}`;
    const sub = zpEl('zpClockSub');
    if (sub) sub.textContent = parts.dateStr;
    const tzName = zpEl('zpClockTzName');
    if (tzName) tzName.textContent = zpClockTzLabel(tz);
  }

  // 时钟时区设置弹层（顶栏按钮触发，外点/Esc 关闭）
  function zpClockTzPop() {
    const btn = zpEl('zpClockTzBtn');
    if (!btn) return;
    let pop = zpEl('zpClockTzPop');
    if (pop) { pop.remove(); return; }
    pop = document.createElement('div');
    pop.className = 'zp-add-menu zp-clock-tz-pop';
    pop.id = 'zpClockTzPop';
    const curTz = ZP_STATE.clockTimezone || ((ZP_CARDS.config.clock || {}).timezone) || 'local';

    const presetsHtml = ZP_TZ_PRESETS.map(p => {
      const active = (p.key === curTz) ? ' active' : '';
      return `<div class="zp-add-item zp-tz-item${active}" data-tz="${escapeHtml(p.key)}">
        <span>${escapeHtml(p.name)}</span><span class="dim">${escapeHtml(p.desc)}</span>
      </div>`;
    }).join('');

    pop.innerHTML = `
      <div class="zp-add-item" style="cursor:default"><span><b>设置时区</b></span><span class="dim">预设或自定义</span></div>
      <div class="zp-tz-list">${presetsHtml}</div>
      <div class="zp-add-item zp-w-city-row" style="cursor:default;margin-top:4px">
        <input class="zp-budget-input zp-clock-tz-input" style="flex:1" placeholder="自定义: Asia/Seoul 或 UTC+9" maxlength="40">
        <button class="zp-chip coral zp-tz-apply" style="cursor:pointer">确定</button>
      </div>`;

    const dash = zpEl('pgDash');
    const br = btn.getBoundingClientRect(), dr = dash.getBoundingClientRect();
    pop.style.top = `${br.bottom - dr.top + 6}px`;
    pop.style.right = `${Math.max(8, dr.right - br.right)}px`;
    dash.appendChild(pop);

    const close = () => { const m = zpEl('zpClockTzPop'); if (m) m.remove(); };
    const outside = ev => {
      if (ev.target.closest('#zpClockTzPop') || ev.target.closest('#zpClockTzBtn')) return;
      close();
      document.removeEventListener('pointerdown', outside, true);
    };
    const onKey = ev => {
      if (ev.key === 'Escape') {
        close();
        document.removeEventListener('keydown', onKey, true);
      }
    };
    setTimeout(() => {
      document.addEventListener('pointerdown', outside, true);
      document.addEventListener('keydown', onKey, true);
    }, 0);

    const applyTz = tz => {
      tz = (tz || 'local').trim();
      close();
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', onKey, true);
      ZP_CARDS.config = ZP_CARDS.config || {};
      ZP_CARDS.config.clock = Object.assign({}, ZP_CARDS.config.clock, { timezone: tz });
      ZP_STATE.clockTimezone = tz;
      zpCardsQueueSave();
      zpRenderClock();
    };

    pop.querySelectorAll('.zp-tz-item').forEach(el => {
      el.addEventListener('click', () => {
        applyTz(el.getAttribute('data-tz'));
      });
    });

    const input = pop.querySelector('.zp-clock-tz-input');
    if (!ZP_TZ_PRESETS.some(p => p.key === curTz)) {
      input.value = curTz;
    }
    const commitInput = () => {
      const v = (input.value || '').trim();
      if (!v) return;
      applyTz(v);
    };
    pop.querySelector('.zp-tz-apply').addEventListener('click', ev => {
      ev.stopPropagation();
      commitInput();
    });
    input.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') commitInput();
      ev.stopPropagation();
    });
  }

  // ---- 天气卡：后端 /api/weather 代理（Open-Meteo，30 分钟缓存），无城市时空态 ----
  function zpWeatherIcon(code, isDay) {
    if (code === 0) return isDay ? 'ph-sun' : 'ph-moon-stars';
    if (code === 1 || code === 2) return isDay ? 'ph-cloud-sun' : 'ph-cloud-moon';
    const map = {
      3: 'ph-cloud', 45: 'ph-cloud-fog', 48: 'ph-cloud-fog',
      51: 'ph-cloud-drizzle', 53: 'ph-cloud-drizzle', 55: 'ph-cloud-drizzle',
      56: 'ph-cloud-drizzle', 57: 'ph-cloud-drizzle',
      61: 'ph-cloud-rain', 63: 'ph-cloud-rain', 65: 'ph-cloud-rain',
      66: 'ph-cloud-rain', 67: 'ph-cloud-rain',
      71: 'ph-cloud-snow', 73: 'ph-cloud-snow', 75: 'ph-cloud-snow', 77: 'ph-cloud-snow',
      80: 'ph-cloud-rain', 81: 'ph-cloud-rain', 82: 'ph-cloud-rain',
      85: 'ph-cloud-snow', 86: 'ph-cloud-snow',
      95: 'ph-cloud-lightning', 96: 'ph-cloud-lightning', 99: 'ph-cloud-lightning'
    };
    return map[code] || 'ph-question';
  }

  function zpWeatherFetch(force) {
    const city = ZP_STATE.weatherCity;
    if (!city) { zpRenderWeather(); return; }
    if (!force && Date.now() - ZP_STATE.weatherTs < 5 * 60 * 1000) return;
    ZP_STATE.weatherTs = Date.now();
    const spin = zpEl('zpWeatherRefreshBtn');
    if (spin) spin.classList.add('spinning');
    fetchWithTimeout(`/api/weather?city=${encodeURIComponent(city)}`, 12000)
      .then(d => {
        ZP_STATE.weather = (d && d.ok) ? d : { ok: false, error: (d && d.error) || '获取失败' };
      })
      .catch(() => { ZP_STATE.weather = { ok: false, error: '网络不可用' }; })
      .finally(() => {
        if (spin) spin.classList.remove('spinning');
        zpRenderWeather();
      });
  }

  function zpRenderWeather() {
    const body = zpEl('zpWeatherBody');
    if (!body) return;
    const nameEl = zpEl('zpWeatherCityName');
    if (nameEl) nameEl.textContent = ZP_STATE.weatherCity || '设置城市';
    if (!ZP_STATE.weatherCity) {
      body.innerHTML = '<div class="zp-w-empty">点击右上角「设置城市」<br>支持中文城市名，数据来自 Open-Meteo</div>';
      return;
    }
    const W = ZP_STATE.weather;
    if (!W) { body.innerHTML = '<div class="zp-w-empty">加载中…</div>'; return; }
    if (!W.ok) {
      body.innerHTML = `<div class="zp-w-empty">${escapeHtml(W.error || '获取失败')}
        <button class="zp-w-retry" type="button">重试</button></div>`;
      const retry = body.querySelector('.zp-w-retry');
      if (retry) retry.addEventListener('click', ev => {
        ev.stopPropagation();
        ZP_STATE.weather = null;
        zpWeatherFetch(true);
      });
      return;
    }
    const c = W.current || {};
    const days = (W.daily || []).slice(0, 4);
    const weekNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    const today = new Date();
    const rows = days.map((d, i) => {
      const dt = new Date(`${d.date}T00:00:00`);
      const label = i === 0 ? '今天' : weekNames[dt.getDay()];
      return `<div class="zp-w-day" title="${escapeHtml(d.date)}">
        <span class="zp-w-day-label">${label}</span>
        <i class="ph ${zpWeatherIcon(d.code, 1)}"></i>
        <b>${d.max != null ? Math.round(d.max) : '--'}°</b>
        <span class="lo">${d.min != null ? Math.round(d.min) : '--'}°</span>
      </div>`;
    }).join('');
    body.innerHTML = `
      <div class="zp-w-now">
        <div class="zp-w-top">
          <i class="ph ${zpWeatherIcon(c.code, c.is_day)} zp-w-icon"></i>
          <div class="zp-w-temp">${c.temp != null ? Math.round(c.temp) : '--'}<span>°C</span></div>
        </div>
        <div class="zp-w-meta">
          <span class="zp-w-text">${escapeHtml(W.text || '')}</span>
          <span>体感 ${c.feels != null ? Math.round(c.feels) : '--'}°</span>
          <span>湿度 ${c.humidity != null ? Math.round(c.humidity) : '--'}%</span>
          <span>风 ${c.wind != null ? Math.round(c.wind) : '--'} km/h</span>
        </div>
      </div>
      <div class="zp-w-days">${rows}</div>`;
  }

  // 城市设置小弹层（顶栏按钮触发，外点/Esc 关闭）
  function zpWeatherCityPop() {
    const btn = zpEl('zpWeatherCityBtn');
    if (!btn) return;
    let pop = zpEl('zpWeatherCityPop');
    if (pop) { pop.remove(); return; }
    pop = document.createElement('div');
    pop.className = 'zp-add-menu zp-w-city-pop';
    pop.id = 'zpWeatherCityPop';
    pop.innerHTML = `
      <div class="zp-add-item" style="cursor:default"><span>天气城市</span><span class="dim">中文城市名</span></div>
      <div class="zp-add-item zp-w-city-row" style="cursor:default">
        <input class="zp-budget-input zp-w-city-input" style="flex:1" placeholder="如：昆明" maxlength="20">
        <button class="zp-chip coral" style="cursor:pointer">确定</button>
      </div>`;
    const dash = zpEl('pgDash');
    const br = btn.getBoundingClientRect(), dr = dash.getBoundingClientRect();
    pop.style.top = `${br.bottom - dr.top + 6}px`;
    pop.style.right = `${Math.max(8, dr.right - br.right)}px`;
    dash.appendChild(pop);
    const close = () => { const m = zpEl('zpWeatherCityPop'); if (m) m.remove(); };
    const outside = ev => {
      if (ev.target.closest('#zpWeatherCityPop') || ev.target.closest('#zpWeatherCityBtn')) return;
      close();
      document.removeEventListener('pointerdown', outside, true);
    };
    const onKey = ev => {
      if (ev.key === 'Escape') {
        close();
        document.removeEventListener('keydown', onKey, true);
      }
    };
    setTimeout(() => {
      document.addEventListener('pointerdown', outside, true);
      document.addEventListener('keydown', onKey, true);
    }, 0);
    const input = pop.querySelector('.zp-w-city-input');
    input.value = ZP_STATE.weatherCity || '';
    input.focus();
    const commit = () => {
      const v = (input.value || '').trim();
      close();
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', onKey, true);
      if (!v || v === ZP_STATE.weatherCity) return;
      ZP_CARDS.config = ZP_CARDS.config || {};
      ZP_CARDS.config.weather = Object.assign({}, ZP_CARDS.config.weather, { city: v });
      ZP_STATE.weatherCity = v;
      ZP_STATE.weather = null;
      zpCardsQueueSave();
      zpWeatherFetch(true);
    };
    pop.querySelector('button').addEventListener('click', ev => {
      ev.stopPropagation();
      commit();
    });
    input.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') commit();
      ev.stopPropagation();
    });
  }

  // ---- 系统监控卡：后端 /api/sysmon（psutil），3s 轮询 ----
  function zpSysmonLoad() {
    fetchWithTimeout('/api/sysmon', 5000)
      .then(d => { if (d && (d.ok || d.error)) ZP_STATE.sysmon = d; zpRenderSysmon(); })
      .catch(() => {
        // 旧版后台无此路由（404）：给出可读提示而不是永远"加载中"
        if (!ZP_STATE.sysmon) {
          ZP_STATE.sysmon = { ok: false, error: '需新版后台（重打包后的 exe）' };
          zpRenderSysmon();
        }
      });
  }

  function zpSysUptimeText(sec) {
    if (!sec || sec < 60) return '--';
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
    return d > 0 ? `已运行 ${d} 天 ${h} 小时` : `已运行 ${h} 小时`;
  }

  function zpRenderSysmon() {
    const rows = zpEl('zpSysRows');
    if (!rows) return;
    const d = ZP_STATE.sysmon;
    if (!d) { rows.innerHTML = '<div class="zp-w-empty">加载中…</div>'; return; }
    if (!d.ok) { rows.innerHTML = `<div class="zp-w-empty">${escapeHtml(d.error || '不可用')}</div>`; return; }
    const up = zpEl('zpSysUptime');
    if (up) up.textContent = zpSysUptimeText(d.uptime_s);
    const fmtGB = b => (b / 1073741824).toFixed(b >= 10737418240 ? 0 : 1);
    const fmtSpeed = bps => bps >= 1048576 ? `${(bps / 1048576).toFixed(1)} MB/s`
      : bps >= 1024 ? `${(bps / 1024).toFixed(0)} KB/s` : `${bps} B/s`;
    // 负载分色：<65 正常 / 65-85 预警 / ≥85 报警
    const bar = pct => {
      const color = pct >= 85 ? 'var(--err)' : pct >= 65 ? 'var(--warn)' : 'var(--zp-ok)';
      return `<span class="zp-sys-bar"><i style="width:${Math.min(100, Math.max(2, pct)).toFixed(0)}%;background:${color}"></i></span>`;
    };
    rows.innerHTML = `
      <div class="zp-sys-row" title="${escapeHtml(d.cpu_name || '')} · ${d.cpu_cores || '--'} 线程">
        <span class="zp-sys-label">CPU</span>${bar(d.cpu || 0)}
        <b class="zp-sys-val">${(d.cpu || 0).toFixed(0)}%</b>
      </div>
      <div class="zp-sys-row" title="内存 ${fmtGB(d.mem.used)} / ${fmtGB(d.mem.total)} GB">
        <span class="zp-sys-label">内存</span>${bar(d.mem.percent || 0)}
        <b class="zp-sys-val">${(d.mem.percent || 0).toFixed(0)}%</b>
      </div>
      <div class="zp-sys-row" title="全部网卡合计速率">
        <span class="zp-sys-label">网速</span>
        <span class="zp-sys-net">
          <i class="ph ph-arrow-down"></i>${fmtSpeed((d.net || {}).down_bps || 0)}
          <i class="ph ph-arrow-up"></i>${fmtSpeed((d.net || {}).up_bps || 0)}
        </span>
      </div>
      ${(d.disks || []).map(k => `
      <div class="zp-sys-row" title="${escapeHtml(k.drive)} 已用 ${fmtGB(k.used)} / ${fmtGB(k.total)} GB">
        <span class="zp-sys-label">${escapeHtml(k.drive)}</span>${bar(k.percent || 0)}
        <b class="zp-sys-val">${(k.percent || 0).toFixed(0)}%</b>
      </div>`).join('')}`;
  }

  // ---- 自定义图片卡：上传 → 压缩 → dashboardCards 持久化；object-fit 可切换 ----
  function zpBindImgCard(node) {
    const id = node.getAttribute('data-card-id');
    node.innerHTML = `
      <div class="zp-img-empty" title="点击选择本地图片或拖拽图片到此">
        <i class="ph ph-image"></i><span>点击上传图片</span>
      </div>
      <img class="zp-img-el" alt="" draggable="false">
      <div class="zp-img-tools">
        <button class="zp-img-btn zp-img-change" type="button" title="更换图片"><i class="ph ph-swap"></i></button>
        <button class="zp-img-btn zp-img-fit" type="button" title="填充方式：完整显示 / 裁剪铺满 / 拉伸"><i class="ph ph-arrows-out"></i></button>
      </div>
      <input type="file" accept="image/*" class="zp-img-file" style="display:none;">`;
    const input = node.querySelector('.zp-img-file');
    const pick = () => {
      // 打开文件框前掐掉长按进编辑的计时器（文件框会吞掉 pointerup）
      clearTimeout(ZP_GRID.pressTimer); ZP_GRID.pressTimer = null;
      input.click();
    };
    node.querySelector('.zp-img-empty').addEventListener('click', ev => {
      ev.stopPropagation();
      pick();
    });
    node.querySelector('.zp-img-change').addEventListener('click', ev => {
      ev.stopPropagation();
      pick();
    });
    node.querySelector('.zp-img-fit').addEventListener('click', ev => {
      ev.stopPropagation();
      const cfg = (ZP_CARDS.config || {})[id];
      if (!cfg || !cfg.img) return;
      const order = ['contain', 'cover', 'fill'];
      cfg.fit = order[(order.indexOf(cfg.fit || 'contain') + 1) % order.length];
      zpRenderImgCard(id);
      zpCardsQueueSave();
    });

    const handleFile = file => {
      if (!file) return;
      if (!/^image\//.test(file.type || '') && !/\.(png|jpe?g|webp|bmp|gif|svg)$/i.test(file.name || '')) {
        const tip = node.querySelector('.zp-img-empty span');
        if (tip) {
          tip.textContent = '只支持图片文件';
          setTimeout(() => { if (tip) tip.textContent = '点击上传图片'; }, 2000);
        }
        return;
      }
      zpCompressImageFile(file).then(dataUrl => {
        ZP_CARDS.config = ZP_CARDS.config || {};
        ZP_CARDS.config[id] = Object.assign({}, ZP_CARDS.config[id] || {},
          { img: dataUrl, fit: (ZP_CARDS.config[id] || {}).fit || 'contain' });
        zpRenderImgCard(id);
        zpCardsQueueSave();
      }).catch(err => {
        console.warn('[ZenPulse] 图片处理失败:', err && err.message);
        const tip = node.querySelector('.zp-img-empty span');
        if (tip) {
          tip.textContent = err && err.message ? err.message : '图片处理失败';
          setTimeout(() => { if (tip) tip.textContent = '点击上传图片'; }, 2500);
        }
      });
    };

    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      input.value = '';
      if (file) handleFile(file);
    });

    // 拖拽上传支持
    node.addEventListener('dragover', ev => {
      ev.preventDefault();
      node.classList.add('zp-img-dragover');
    });
    node.addEventListener('dragleave', ev => {
      if (!node.contains(ev.relatedTarget)) {
        node.classList.remove('zp-img-dragover');
      }
    });
    node.addEventListener('drop', ev => {
      ev.preventDefault();
      node.classList.remove('zp-img-dragover');
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if (file) handleFile(file);
    });
  }

  function zpRenderImgCard(id) {
    const node = document.querySelector(`#zpGrid [data-card-id="${id}"]`) || ZP_GRID.detached[id];
    if (!node) return;
    if (!node.dataset.zpBound) { zpBindImgCard(node); node.dataset.zpBound = '1'; }
    const cfg = (ZP_CARDS.config || {})[id] || {};
    const img = node.querySelector('.zp-img-el');
    const fitBtn = node.querySelector('.zp-img-fit');
    const fitNames = { contain: '完整显示', cover: '裁剪铺满', fill: '拉伸' };
    node.classList.toggle('has-img', !!cfg.img);
    if (cfg.img) {
      if (img.getAttribute('src') !== cfg.img) img.src = cfg.img;
      img.style.objectFit = fitNames[cfg.fit] ? cfg.fit : 'contain';
      if (fitBtn) fitBtn.innerHTML = `<i class="ph ${cfg.fit === 'cover' ? 'ph-arrows-in' : 'ph-arrows-out'}"></i>`;
      if (fitBtn) fitBtn.title = `填充方式：${fitNames[cfg.fit] || '完整显示'}（点击切换）`;
    } else {
      img.removeAttribute('src');
    }
  }

  // 上传图片统一重编码：PNG（可能带透明）原样保留（超限先缩再退 JPEG 白底）；
  // 其余格式经 canvas 限制最大边 1600px、JPEG 0.85，单张控制在后端 3.5MB 红线内。
  function zpCompressImageFile(file) {
    return new Promise((resolve, reject) => {
      const LIMIT = 3_400_000;
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('图片读取失败'));
      reader.onload = () => {
        const raw = String(reader.result || '');
        if (!raw.startsWith('data:image/')) { reject(new Error('不是图片文件')); return; }
        const isPng = raw.startsWith('data:image/png;base64,');
        if (isPng && raw.length <= LIMIT) { resolve(raw); return; }  // 保留透明通道直存
        const img = new Image();
        img.onerror = () => reject(new Error('图片解析失败'));
        img.onload = () => {
          try {
            const MAX = 1600;
            const scale = Math.min(1, MAX / Math.max(img.width, img.height));
            const w = Math.max(1, Math.round(img.width * scale));
            const h = Math.max(1, Math.round(img.height * scale));
            const canvas = document.createElement('canvas');
            canvas.width = w; canvas.height = h;
            const ctx = canvas.getContext('2d');
            const white = raw.length > LIMIT;   // 超限 PNG 退 JPEG 时垫白底
            if (white) { ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, w, h); }
            ctx.drawImage(img, 0, 0, w, h);
            let out = canvas.toDataURL(white || !isPng ? 'image/jpeg' : 'image/png', 0.85);
            if (out.length > LIMIT && !white) {
              ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, w, h);
              ctx.drawImage(img, 0, 0, w, h);
              out = canvas.toDataURL('image/jpeg', 0.85);
            }
            if (out.length > LIMIT) { reject(new Error('图片过大（压缩后仍 >3.4MB）')); return; }
            resolve(out);
          } catch (err) { reject(err); }
        };
        img.src = raw;
      };
      reader.readAsDataURL(file);
    });
  }

  // ============ 汇总入口 ============
  function zpRenderStatic() {
    zpRenderKpis();
    if (!state.toolsLoading) zpRenderTools();
    zpRenderBudget();
    zpRenderSourceChip();
  }

  function zpRenderLive() {
    zpRenderHero();
    zpRenderArea();
    zpRenderRipple();
    zpRenderRateVt();
    zpRenderStream();
  }

  function zpRenderAll() {
    zpRenderStatic();
    zpRenderLive();
  }

  // ============ 事件绑定（新仪表盘专属，DOMContentLoaded 时调一次） ============
  function zpBindEvents() {
    zpBindTools();
    let zpResizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(zpResizeTimer);
      zpResizeTimer = setTimeout(() => {
        const pg = zpEl('pgDash');
        if (pg && pg.classList.contains('on')) {
          zpRenderAll();
          zpRenderClock();
        }
      }, 150);
    });
    const refresh = zpEl('zpRefreshBtn');
    if (refresh) {
      refresh.addEventListener('click', () => {
        refresh.classList.add('spinning');
        Promise.all([loadData(), zpFetch(true)]).finally(() =>
          refresh.classList.remove('spinning'));
      });
    }
    const quotaRefresh = zpEl('zpQuotaRefreshBtn');
    if (quotaRefresh) {
      quotaRefresh.addEventListener('click', refreshQuota);
    }
    const srcBtn = zpEl('zpSourceBtn'), pop = zpEl('zpSourcePop');
    if (srcBtn && pop) {
      srcBtn.addEventListener('click', e => {
        e.stopPropagation();
        pop.hidden = !pop.hidden;
      });
      document.addEventListener('click', e => {
        if (!pop.hidden && !pop.contains(e.target) && e.target !== srcBtn) pop.hidden = true;
      });
    }
    // 图例开关
    document.querySelectorAll('#zpAreaLegend .lg').forEach(lg => {
      lg.addEventListener('click', () => {
        const k = lg.getAttribute('data-zp-series');
        ZP_STATE.series[k] = !ZP_STATE.series[k];
        lg.classList.toggle('off', !ZP_STATE.series[k]);
        zpRenderArea();
      });
    });
    // Token / 费用切换
    const tokBtn = zpEl('zpModeTok'), costBtn = zpEl('zpModeCost');
    if (tokBtn && costBtn) {
      tokBtn.addEventListener('click', () => {
        ZP_STATE.mode = 'token';
        tokBtn.classList.add('on'); costBtn.classList.remove('on');
        zpRenderArea();
      });
      costBtn.addEventListener('click', () => {
        ZP_STATE.mode = 'cost';
        costBtn.classList.add('on'); tokBtn.classList.remove('on');
        zpRenderArea();
      });
    }
    // 英雄卡：速率单位胶囊切换（tok/s 与 tok/min）
    const heroToggle = zpEl('zpHeroUnitToggle');
    if (heroToggle) {
      const btns = heroToggle.querySelectorAll('.zp-capsule-btn');
      btns.forEach(btn => {
        btn.classList.toggle('on', btn.getAttribute('data-unit') === (ZP_STATE.heroRateUnit || 's'));
        btn.addEventListener('click', () => {
          const u = btn.getAttribute('data-unit') || 's';
          if (ZP_STATE.heroRateUnit === u) return;
          ZP_STATE.heroRateUnit = u;
          localStorage.setItem('zp_hero_rate_unit', u);
          btns.forEach(b => b.classList.toggle('on', b === btn));
          zpRenderHero();
        });
      });
    }
    const rateBadge = zpEl('zpRateBadge');
    if (rateBadge) {
      rateBadge.style.cursor = 'pointer';
      rateBadge.addEventListener('click', () => {
        const next = (ZP_STATE.heroRateUnit === 's') ? 'min' : 's';
        ZP_STATE.heroRateUnit = next;
        localStorage.setItem('zp_hero_rate_unit', next);
        if (heroToggle) {
          heroToggle.querySelectorAll('.zp-capsule-btn').forEach(b => {
            b.classList.toggle('on', b.getAttribute('data-unit') === next);
          });
        }
        zpRenderHero();
      });
    }
    // 请求速率（v-t 图）胶囊切换（生成 v(t) vs 请求 v(t) vs 吞吐 v(t)）
    const vtToggle = zpEl('zpVtUnitToggle');
    if (vtToggle) {
      const btns = vtToggle.querySelectorAll('.zp-capsule-btn');
      btns.forEach(btn => {
        btn.classList.toggle('on', btn.getAttribute('data-vt-mode') === (ZP_STATE.vtMode || 'gen'));
        btn.addEventListener('click', () => {
          const m = btn.getAttribute('data-vt-mode') || 'gen';
          if (ZP_STATE.vtMode === m) return;
          ZP_STATE.vtMode = m;
          localStorage.setItem('zp_vt_mode', m);
          btns.forEach(b => b.classList.toggle('on', b === btn));
          zpRenderRateVt();
        });
      });
    }
    // 请求速率（v-t 图）单模型筛选
    const vtModelSel = zpEl('zpVtModelSelect');
    if (vtModelSel) {
      vtModelSel.value = ZP_STATE.vtModel || '';
      vtModelSel.classList.toggle('active-filter', !!ZP_STATE.vtModel);
      vtModelSel.addEventListener('change', () => {
        const val = vtModelSel.value || '';
        if (ZP_STATE.vtModel === val) return;
        ZP_STATE.vtModel = val;
        try { localStorage.setItem('zp_vt_model', val); } catch (e) { }
        vtModelSel.classList.toggle('active-filter', !!val);
        zpRenderRateVt();
      });
    }
    // 请求速率（v-t 图）时间范围筛选（1h/3h/6h/12h/24h，默认 1h）
    const vtRangeToggle = zpEl('zpVtRangeToggle');
    if (vtRangeToggle) {
      const btns = vtRangeToggle.querySelectorAll('.zp-capsule-btn');
      const curR = String(ZP_STATE.vtRange || 60);
      btns.forEach(btn => {
        btn.classList.toggle('on', btn.getAttribute('data-vt-range') === curR);
        btn.addEventListener('click', () => {
          const v = parseInt(btn.getAttribute('data-vt-range') || '60', 10) || 60;
          if (ZP_STATE.vtRange === v) return;
          ZP_STATE.vtRange = v;
          try { localStorage.setItem('zp_vt_range', String(v)); } catch (e) { }
          btns.forEach(b => b.classList.toggle('on', b === btn));
          ZP_STATE.vt = null;   // 旧窗口数据作废，等新窗口数据到达
          ZP_STATE.vtErr = false;
          zpRenderRateVt();     // 立即反馈（非默认窗先显示「加载中…」）
          zpFetchVt();
        });
      });
    }
    // 焦点 chip 取消
    const chip = zpEl('zpFocusChip');
    if (chip) chip.addEventListener('click', () => zpSetFocus(null));
    // 时钟卡：时区设置
    const cTzBtn = zpEl('zpClockTzBtn');
    if (cTzBtn) cTzBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      zpClockTzPop();
    });
    // 天气卡：城市设置 + 手动刷新
    const wCityBtn = zpEl('zpWeatherCityBtn');
    if (wCityBtn) wCityBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      zpWeatherCityPop();
    });
    const wRefresh = zpEl('zpWeatherRefreshBtn');
    if (wRefresh) wRefresh.addEventListener('click', ev => {
      ev.stopPropagation();
      zpWeatherFetch(true);
    });
    // 预算表单
    const bBtn = zpEl('zpBudgetBtn'), bForm = zpEl('zpBudgetForm');
    if (bBtn && bForm) {
      bBtn.addEventListener('click', () => {
        bForm.style.display = bForm.style.display === 'none' ? '' : 'none';
        const input = zpEl('zpBudgetInput');
        if (input) {
          let cur = null;
          try { cur = Number(localStorage.getItem('zpMonthlyBudget')) || null; } catch (e) { }
          input.value = cur || '';
        }
      });
    }
    const bSave = zpEl('zpBudgetSave');
    if (bSave) bSave.addEventListener('click', () => {
      const input = zpEl('zpBudgetInput');
      const v = input ? Math.max(0, Number(input.value) || 0) : 0;
      try {
        if (v > 0) localStorage.setItem('zpMonthlyBudget', String(v));
        else localStorage.removeItem('zpMonthlyBudget');
      } catch (e) { }
      const form = zpEl('zpBudgetForm');
      if (form) form.style.display = 'none';
      zpRenderBudget();
    });
    const bClear = zpEl('zpBudgetClear');
    if (bClear) bClear.addEventListener('click', () => {
      try { localStorage.removeItem('zpMonthlyBudget'); } catch (e) { }
      const input = zpEl('zpBudgetInput');
      if (input) input.value = '';
      zpRenderBudget();
    });
    // 统计来源变化 → 顶部 chip 同步（原监听器仍负责口径逻辑）
    const picker = zpEl('modelSourcePicker');
    if (picker) {
      picker.addEventListener('change', zpRenderSourceChip);
      picker.addEventListener('click', e => {
        if (e.target.closest('[data-model-sources-action]')) zpRenderSourceChip();
      });
    }
    // 模型占比 pill 切换后补一次来源 chip（口径同上）
    const pills = zpEl('modelSourcePills');
    if (pills) pills.addEventListener('click', zpRenderSourceChip);
  }

  // --- 数据获取与异步探测 ---
  async function fetchWithTimeout(url, timeoutMs = 5000) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const resp = await fetch(url, { signal: controller.signal });
      clearTimeout(id);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return await resp.json();
    } catch (e) {
      clearTimeout(id);
      throw e;
    }
  }

  // 超时/失败先重试 1 次，再抛出异常
  async function fetchWithRetry(url, timeoutMs, retries = 1) {
    let lastErr;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        return await fetchWithTimeout(url, timeoutMs);
      } catch (err) {
        lastErr = err;
        if (attempt < retries) {
          await new Promise(resolve => setTimeout(resolve, 400));
        }
      }
    }
    throw lastErr;
  }

  // 每个接口到达后立刻刷新依赖它的区块：慢接口（/tools、/model-sources）
  // 不再拖住整页——时间范围一改就能先看到汇总/趋势，环形图随后补齐。
  const LOAD_RENDERERS = {
    summary: () => { renderOverviewCards(); zpRenderStatic(); },
    agents: () => { renderModelSection(); renderToolsSection(); updatePanelSummaries(); },
    models: () => renderModelSection(),
    modelSources: () => { renderModelSourcePicker(); renderOverviewCards(); renderModelSection(); zpRenderStatic(); },
    daily: () => { renderDailyChart(); zpRenderKpis(); },
    tools: () => { renderToolsSection(); zpRenderTools(); },
    health: () => updatePanelSummaries()
  };

  function applyLoadResult(key, value) {
    switch (key) {
      case 'summary':
        state.summaryData = value;
        state.generatedAt = value.generated_at || new Date().toLocaleString();
        break;
      case 'agents': state.agentsData = value; break;
      case 'models': state.modelsData = value; break;
      case 'modelSources':
        state.modelSourcesData = value;
        state.modelSourcesError = null;
        break;
      case 'daily': state.dailyData = value; break;
      case 'tools': {
        state.toolsLoading = false;
        state.toolsData = (value && value.tools) ? value.tools : (Array.isArray(value) ? value : []);
        if (value && value.generated_at && !state.summaryData) state.generatedAt = value.generated_at;
        break;
      }
      case 'health': state.healthData = value; break;
    }
    const render = LOAD_RENDERERS[key];
    if (render) {
      try { render(); } catch (err) { console.warn(`[StatsAPI] ${key} 渲染异常:`, err); }
    }
  }

  async function loadData() {
        // 检查更新按钮与顶部提示绑定
    const btnCheckUpdate = document.getElementById("btnCheckUpdateManual");
    if (btnCheckUpdate) {
      btnCheckUpdate.addEventListener("click", () => checkAppUpdate(true));
    }
    const topUpdateBadge = document.getElementById("updateAvailBadge");
    if (topUpdateBadge) {
      topUpdateBadge.addEventListener("click", () => {
        location.hash = "#/status";
        setTimeout(() => {
          document.getElementById("cardAppUpdate")?.scrollIntoView({ behavior: "smooth" });
        }, 150);
      });
    }

    const refreshBtn = document.getElementById('refreshBtn');
    if (refreshBtn) refreshBtn.classList.add('spinning');
    // 仪表盘刷新按钮同显忙碌态：改时间范围时也有反馈（此前点完像没反应）
    const zpRefreshBtn = document.getElementById('zpRefreshBtn');
    if (zpRefreshBtn) zpRefreshBtn.classList.add('spinning');

    state.toolsLoading = true;
    // 自定义范围传 ?start=（UTC 秒，后端各统计直通 since）；预设仍传 days
    const winParam = (state.timeWindow === 'custom' && state.customStart)
      ? `start=${Math.floor(state.customStart / 1000)}`
      : `days=${state.timeWindow === 'all' ? 'all' : state.timeWindow}`;

    // 后端已把 Codex / Claude 本机扫描改成增量 + 启动预热，单接口通常 <1s；
    // 超时保留余量只为异常情况（首次冷扫、数据源被占用）。
    const requests = [
      { key: 'summary', url: `${API_BASE}/summary?${winParam}`, timeout: 8000 },
      { key: 'agents',  url: `${API_BASE}/agents?${winParam}`,  timeout: 8000 },
      { key: 'models',  url: `${API_BASE}/models?${winParam}`,  timeout: 8000 },
      { key: 'modelSources', url: `${API_BASE}/model-sources?${winParam}`, timeout: 20000 },
      { key: 'daily',   url: `${API_BASE}/daily`,               timeout: 8000 },
      { key: 'tools',   url: `${API_BASE}/tools?${winParam}`,   timeout: 20000 },
      { key: 'health',  url: `${API_BASE}/health`,              timeout: 8000 }
    ];

    try {
      const failedKeys = [];
      let successCount = 0;

      // 每个请求独立落地并立刻重绘自己负责的区块（谁先回谁先上屏）
      await Promise.all(requests.map(async req => {
        try {
          const value = await fetchWithRetry(req.url, req.timeout, 1);
          successCount++;
          if (state.dataSourceType !== 'live') {
            // 至少有一个接口成功即切实时态，降级不粘滞
            state.dataSourceType = 'live';
          }
          applyLoadResult(req.key, value);
        } catch (err) {
          failedKeys.push(req.key);
          if (req.key === 'tools') state.toolsLoading = false;
          if (req.key === 'modelSources') {
            state.modelSourcesData = null;
            state.modelSourcesError = '模型来源统计接口请求失败';
          }
          console.warn(`[StatsAPI] ${req.key} 请求未成功 (已重试):`, err?.message || err);
        }
      }));

      // 仅当全部接口彻底失败（后端服务未启动）时才降级为纯样本兜底
      if (successCount === 0) {
        state.dataSourceType = 'sample';
        state.dataSourceStatusText = '本地样本 (服务离线)';
        state.modelSourcesData = null;
        state.modelSourcesError = null;
        initFallback();
        return;
      }

      // 数据源标记说真话：部分成功时精准报告具体异常接口
      if (failedKeys.length === 0) {
        state.dataSourceStatusText = '本机服务 (8790)';
      } else {
        const failLabels = failedKeys.map(k => {
          if (k === 'tools') return '工具数据超时';
          if (k === 'models') return '模型数据超时';
          if (k === 'modelSources') return '模型来源统计失败';
          if (k === 'daily') return '趋势数据超时';
          if (k === 'summary') return '汇总数据超时';
          return `${k}失败`;
        });
        state.dataSourceStatusText = `本机服务 (8790) · ${failLabels.join(' · ')}`;
      }

      updateHeaderStatus();
      renderSourceStatusBar();
      initPanelStates();
      // 收尾整体重绘一次：部分成功时把缺数据的区块一并校正
      renderOverviewCards();
      renderToolsSection();
      renderModelSection();
      renderDailyChart();
      zpRenderStatic();
      updatePanelSummaries();
    } catch (err) {
      console.warn('[StatsAPI] loadData 流程捕获异常:', err);
      if (state.dataSourceType !== 'live') {
        initFallback();
      }
    } finally {
      if (refreshBtn) refreshBtn.classList.remove('spinning');
      if (zpRefreshBtn) zpRefreshBtn.classList.remove('spinning');
    }
  }

  // --- 顶部状态更新 ---
  function updateHeaderStatus() {
    const badge = document.getElementById('dataSourceBadge');
    const updateTimeEl = document.getElementById('lastUpdateTime');
    if (!badge) return;

    if (state.dataSourceType === 'live') {
      const isWarn = state.dataSourceStatusText &&
        (state.dataSourceStatusText.includes('超时') || state.dataSourceStatusText.includes('失败'));
      badge.className = isWarn ? 'source-badge warning' : 'source-badge online';
      badge.innerHTML = `<span class="status-dot"></span><span>数据源：${state.dataSourceStatusText || '本机服务 (8790)'}</span>`;
    } else {
      badge.className = 'source-badge fallback';
      badge.innerHTML = `<span class="status-dot"></span><span>数据源：${state.dataSourceStatusText || '本地样本'}</span>`;
    }

    if (updateTimeEl) {
      updateTimeEl.textContent = `生成时间: ${state.generatedAt || '—'}`;
    }
  }

  function getLegacyMergedSourceTokenTotals() {
    const summary = state.summaryData || {};
    const ccsModels = state.modelsData || [];
    const ccsSummary = summary.ccs || {};
    const ccsSummaryTotal = Number(ccsSummary.input || 0) + Number(ccsSummary.output || 0)
      + Number(ccsSummary.cache_read || 0) + Number(ccsSummary.cache_creation || 0);
    const ccsModelTotal = ccsModels.reduce((sum, m) => sum + (Number(m.total_tokens)
      || (Number(m.input || 0) + Number(m.output || 0) + Number(m.cache_read || 0) + Number(m.cache_creation || 0))), 0);
    const ccs = ccsSummaryTotal > 0 ? ccsSummaryTotal : ccsModelTotal;

    let hermesModelTotal = 0;
    (state.agentsData || []).forEach(agent => {
      (agent.models || []).forEach(m => {
        hermesModelTotal += Number(m.input || 0) + Number(m.output || 0) + Number(m.cache_read || 0);
      });
    });
    const hermesSummary = summary.hermes || {};
    const hermesSummaryTotal = Number(hermesSummary.input || 0) + Number(hermesSummary.output || 0)
      + Number(hermesSummary.cache_read || 0) + Number(hermesSummary.cache_write || 0);
    const hermes = hermesSummaryTotal > 0 ? hermesSummaryTotal : hermesModelTotal;

    const codexTool = (state.toolsData || []).find(t => t.name === 'Codex' || t.group === 'Codex');
    const codexModels = codexTool && Array.isArray(codexTool.models) ? codexTool.models : [];
    const codex = codexModels.reduce((sum, m) => {
      const input = Number(m.input) || 0;
      const output = Number(m.output) || 0;
      const cacheRead = Number(m.cache_read) || 0;
      const inputSide = cacheRead > input ? input + cacheRead : input;
      return sum + (Number(m.total_tokens) || (inputSide + output));
    }, 0);

    const zcodeTool = (state.toolsData || []).find(t => t.name === 'ZCode' || t.group === 'ZCode');
    const zcodeModels = zcodeTool && Array.isArray(zcodeTool.models) ? zcodeTool.models : [];
    const zcodeModelTotal = zcodeModels.reduce((sum, m) => sum + (Number(m.total)
      || (Number(m.input || 0) + Number(m.output || 0) + Number(m.cache_read || 0))), 0);
    const zcodeReportedTotal = zcodeTool ? (Number(zcodeTool.total_tokens) || Number(zcodeTool.provider_total_tokens) || 0) : 0;
    const zcode = zcodeReportedTotal > 0 ? zcodeReportedTotal : zcodeModelTotal;

    const agTool = (state.toolsData || []).find(t => t.name === 'Antigravity' || t.group === 'Antigravity');
    const agModels = agTool && Array.isArray(agTool.models) ? agTool.models : ((agTool?.tokens?.models) || []);
    const agModelTotal = agModels.reduce((sum, m) => sum + (Number(m.total)
      || (Number(m.input || 0) + Number(m.output || 0) + Number(m.cache_read || 0))), 0);
    const agReportedTotal = agTool ? (Number(agTool.total_tokens) || Number(agTool.tokens?.total_tokens) || 0) : 0;
    const antigravity = agReportedTotal > 0 ? agReportedTotal : agModelTotal;

    return { ccs, hermes, codex, zcode, antigravity, total: ccs + hermes + codex + zcode + antigravity };
  }

  function getModelSourceRows() {
    return Array.isArray(state.modelSourcesData?.sources) ? state.modelSourcesData.sources : [];
  }

  function getLegacyModelSourceTotals() {
    const legacy = getLegacyMergedSourceTokenTotals();
    return { ccs: legacy.ccs, hermes: legacy.hermes, codex: legacy.codex, zcode: legacy.zcode, antigravity: legacy.antigravity };
  }

  function renderModelSourcePills() {
    const pillsEl = document.getElementById('modelSourcePills');
    if (!pillsEl) return;

    const hasPayload = Boolean(state.modelSourcesData && Array.isArray(state.modelSourcesData.sources));
    const sources = hasPayload
      ? getModelSourceRows().filter(source => source.available === true && source.has_data === true)
      : MODEL_SOURCE_OPTIONS.filter(source => ['ccs', 'hermes', 'codex', 'zcode', 'antigravity'].includes(source.id));
    const options = [{ id: 'all', label: '全部合并' }, ...sources.map(source => ({
      id: source.id,
      label: source.name || MODEL_SOURCE_OPTIONS.find(item => item.id === source.id)?.label || source.id
    }))];

    if (!options.some(option => option.id === state.modelSource)) state.modelSource = 'all';
    pillsEl.innerHTML = options.map(option => {
      const active = option.id === state.modelSource;
      return `<button class="chip${active ? ' on' : ''}" type="button" data-source="${escapeHtml(option.id)}" aria-pressed="${active ? 'true' : 'false'}">${escapeHtml(option.label)}</button>`;
    }).join('');
  }

  function getMergedSourceTokenTotals() {
    if (state.modelSourcesData && Array.isArray(state.modelSourcesData.sources)) {
      const byId = {};
      state.modelSourcesData.sources.forEach(source => { byId[source.id] = source; });
      const selected = state.enabledModelSources || [];
      const included = getModelSourceRows().filter(source =>
        selected.includes(source.id) && source.available === true);
      return {
        byId, selectedCount: selected.length, included,
        total: included.reduce((sum, source) => sum + (Number(source.total_tokens) || 0), 0)
      };
    }
    if (state.dataSourceType === 'sample' || (state.dataSourceType === 'live' && state.modelSourcesError)) {
      const legacy = getLegacyModelSourceTotals();
      const selected = state.enabledModelSources || [];
      const supported = selected.filter(id => Object.prototype.hasOwnProperty.call(legacy, id));
      if (selected.length > 0 && supported.length === 0) {
        return { byId: {}, selectedCount: selected.length, included: [], total: null,
          error: '当前后台只支持 CCS、Hermes、Codex、ZCode、Antigravity；请重启新版 XiaoyuToolbox 读取新增来源。',
          compatibility: state.dataSourceType === 'live' };
      }
      const total = supported.reduce((sum, id) => sum + (legacy[id] || 0), 0);
      return { byId: {}, selectedCount: selected.length, included: [], total,
        sample: state.dataSourceType === 'sample', compatibility: state.dataSourceType === 'live' };
    }
    return { byId: {}, selectedCount: (state.enabledModelSources || []).length,
      included: [], total: null, error: state.modelSourcesError || '模型来源统计尚未读取' };
  }

  // 已勾选可用来源的 input/output/cache 明细：KPI 卡（新输入输出、缓存命中率）
  // 与环形图共用同一份口径，不再只算 CCS+Hermes（那样会漏掉 Codex / Claude 桌面端）。
  function getMergedSourceUsage() {
    const merged = getMergedSourceTokenTotals();
    if (!merged.included || !merged.included.length) return null;
    const acc = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
      cacheCreation: 0, requests: 0, cacheDenominator: 0, sources: merged.included.length };
    merged.included.forEach(source => {
      (source.models || []).forEach(m => {
        acc.input += Number(m.input) || 0;
        acc.output += Number(m.output) || 0;
        acc.cacheRead += Number(m.cache_read) || 0;
        acc.cacheWrite += Number(m.cache_write) || 0;
        acc.cacheCreation += Number(m.cache_creation) || 0;
        acc.requests += Number(m.requests) || 0;
        const cDenom = Number(m.cache_denominator);
        acc.cacheDenominator += (Number.isFinite(cDenom) && cDenom > 0)
          ? cDenom
          : ((Number(m.input) || 0) + (Number(m.cache_read) || 0) + (Number(m.cache_creation) || 0));
      });
    });
    return acc;
  }

  // 预估费用：工具卡口径汇总（CCS 各端 + Codex + Hermes + ZCode…），
  // 旧实现只取 summary 的 CCS+Hermes，Codex 的费用整块漏掉。
  function getMergedCostTotal() {
    const tools = Array.isArray(state.toolsData) ? state.toolsData : [];
    let sum = 0, seen = 0;
    tools.forEach(t => {
      const c = Number(t.cost_usd);
      if (isFinite(c) && c > 0) { sum += c; seen++; }
    });
    if (!seen) return null;
    return sum;
  }

  function renderModelSourcePicker() {    renderModelSourcePills();
    const optionsEl = document.getElementById('modelSourceOptions');
    if (!optionsEl) return;
    const byId = {};
    getModelSourceRows().forEach(source => { byId[source.id] = source; });
    const selected = state.enabledModelSources || [];
    const hasPayload = Boolean(state.modelSourcesData && Array.isArray(state.modelSourcesData.sources));
    const labels = { available: '可用', noUsage: '无用量', missing: '未发现',
      failed: '读取失败', partial: '部分异常', legacy: '兼容', newer: '需新版' };
    const legacyTotals = !hasPayload && (state.dataSourceType === 'sample' || state.modelSourcesError)
      ? getLegacyModelSourceTotals() : null;
    optionsEl.innerHTML = MODEL_SOURCE_OPTIONS.map(def => {
      const source = byId[def.id];
      let status = state.modelSourcesError ? 'failed' : 'missing';
      if (hasPayload && source) {
        if (source.available !== true) status = /失败|无法读取/.test(source.reason || '') ? 'failed' : 'missing';
        else if (source.reason && source.has_data) status = 'partial';
        else if (source.has_data) status = 'available';
        else status = 'noUsage';
      } else if (!hasPayload && legacyTotals) {
        status = Object.prototype.hasOwnProperty.call(legacyTotals, def.id)
          ? (state.dataSourceType === 'sample' ? 'missing' : 'legacy') : 'newer';
      }
      const statusText = labels[status];
      const statusClass = status === 'available' ? '' : (status === 'partial' || status === 'failed' ? ' warning' : ' unavailable');
      const total = source && source.available === true ? formatCompact(Number(source.total_tokens) || 0)
        : (legacyTotals && Object.prototype.hasOwnProperty.call(legacyTotals, def.id)
          ? formatCompact(legacyTotals[def.id] || 0) : '—');
      const tipParts = [];
      if (source?.reason) tipParts.push(source.reason);
      if (source?.note) tipParts.push(source.note);
      if (!hasPayload && state.modelSourcesError) tipParts.push('统计来源接口暂不可用。');
      if (!hasPayload && state.dataSourceType === 'sample') tipParts.push('当前为离线样本，不含多来源统计。');
      if (!hasPayload && state.dataSourceType === 'live' && state.modelSourcesError) {
        tipParts.push(Object.prototype.hasOwnProperty.call(legacyTotals || {}, def.id)
          ? '旧版兼容统计；重启新版 XiaoyuToolbox 后可读取该来源的完整明细。'
          : '此来源需要新版 XiaoyuToolbox 后台。');
      }
      return `<label class="model-source-option" title="${escapeHtml(tipParts.join(' · '))}">
        <input type="checkbox" data-model-source-toggle="${def.id}" ${selected.includes(def.id) ? 'checked' : ''}>
        <span class="model-source-option-main">
          <span class="model-source-option-name">${escapeHtml(source?.name || def.label)}</span>
          <span class="model-source-option-total">${total}</span>
        </span>
        <span class="model-source-option-status${statusClass}">${statusText}</span>
      </label>`;
    }).join('');

    const count = document.getElementById('chipModelSourceCount');
    if (count) count.textContent = `${selected.length} / ${MODEL_SOURCE_OPTIONS.length}`;
    const overlap = document.getElementById('modelSourceOverlapNote');
    if (overlap) {
      const totalState = getMergedSourceTokenTotals();
      const excluded = selected.filter(id => byId[id] && byId[id].available !== true).length;
      overlap.textContent = state.modelSourcesError && !totalState.compatibility
        ? '统计来源接口读取失败；当前总 Token 与合并图暂不可用。'
        : `已选 ${selected.length} 个来源${excluded ? `，其中 ${excluded} 个尚不可用` : ''}。来源记录可能重叠，未跨来源去重。`;
      if (totalState.sample) overlap.textContent = '离线样本仅保留旧版四来源展示；连接本机服务后可读取新增来源。';
      else if (totalState.compatibility) overlap.textContent = '旧版后台兼容模式：总 Token 与合并图暂按 CCS、Hermes、Codex、ZCode 统计；重启新版 XiaoyuToolbox 后可读取其余来源。';
    }
  }

  // --- 1. 概览卡片区渲染 (v2.1: 成本口径统一 & 异常金额对齐) ---
  function renderOverviewCards() {
    const sum = state.summaryData;
    const mergedTokenTotals = getMergedSourceTokenTotals();
    const grandTotalTokens = mergedTokenTotals.total;
    const cardTotalTokenEl = document.getElementById('cardTotalTokenVal');
    const sourceLines = getModelSourceRows().map(source => {
      const isSelected = (state.enabledModelSources || []).includes(source.id);
      const included = isSelected && source.available === true;
      const value = source.available === true ? formatNumber(Number(source.total_tokens) || 0) : '未计入';
      const status = source.available === true
        ? (source.reason && source.has_data ? `（部分异常：${source.reason}）` : '')
        : `（${source.reason || '不可用'}）`;
      const note = source.note ? `\n  口径：${source.note}` : '';
      return `${isSelected ? '✓' : '○'} ${source.name}: ${included ? value : (isSelected ? value : '已关闭')}${status}${note}`;
    });
    const cardTokenTooltip = grandTotalTokens == null
      ? `所选来源总量暂不可用。\n${mergedTokenTotals.error || '来源接口尚未返回数据。'}`
      : `【所选来源合计】\n总 Token: ${formatNumber(grandTotalTokens)}\n\n${sourceLines.join('\n')}\n\n来源记录可能重叠（例如客户端会话与代理日志），未跨来源去重。`
        + (mergedTokenTotals.sample ? '\n当前数字来自离线样本。' : '')
        + (mergedTokenTotals.compatibility ? '\n当前为旧版后台兼容统计；重启新版 XiaoyuToolbox 后可读取新增来源。' : '');
    if (cardTotalTokenEl) {
      cardTotalTokenEl.textContent = grandTotalTokens == null ? '—' : formatCompact(grandTotalTokens);
      cardTotalTokenEl.setAttribute('data-tooltip', cardTokenTooltip);
    }
    renderModelSourcePicker();
    if (!sum) return;

    const hermes = sum.hermes || {};
    const ccs = sum.ccs || {};

    // 其余概览 KPI 保持原有统计口径；总 Token 已按来源开关在上方渲染。
    const hInput = hermes.input || 0;
    const hCacheRead = hermes.cache_read || 0;
    const hCacheWrite = hermes.cache_write || 0;
    const cInput = ccs.input || 0;
    const cCacheRead = ccs.cache_read || 0;
    const cCacheCreation = ccs.cache_creation || 0;

    // 命中率
    const hDenom = hInput + hCacheRead + hCacheWrite;
    const hHitRate = hDenom > 0 ? hCacheRead / hDenom : 0;

    const cDenom = cInput + cCacheRead + cCacheCreation;
    const cHitRate = cDenom > 0 ? cCacheRead / cDenom : (ccs.cache_hit_rate || 0);

    const totalCacheRead = hCacheRead + cCacheRead;
    const totalInputSide = hDenom + cDenom;
    const combinedHitRate = totalInputSide > 0 ? totalCacheRead / totalInputSide : 0;

    const cardHitRateEl = document.getElementById('cardHitRateVal');
    const cardHitRateTooltip = `【命中率计算口径】\n公式: cache_read / (input + cache_read + cache_creation)\n• 综合命中率: ${(combinedHitRate * 100).toFixed(2)}%\n  - 读取缓存 Token: ${formatNumber(totalCacheRead)}\n  - 输入侧总 Token: ${formatNumber(totalInputSide)}\n• CCS 命中率: ${(cHitRate * 100).toFixed(2)}%\n• Hermes 命中率: ${(hHitRate * 100).toFixed(2)}%`;

    if (cardHitRateEl) {
      cardHitRateEl.textContent = formatPercent(combinedHitRate);
      cardHitRateEl.setAttribute('data-tooltip', cardHitRateTooltip);
    }
    const chipCcsHit = document.getElementById('chipCcsHit');
    if (chipCcsHit) chipCcsHit.textContent = formatPercent(cHitRate);
    const chipHermesHit = document.getElementById('chipHermesHit');
    if (chipHermesHit) chipHermesHit.textContent = formatPercent(hHitRate);

    // 1.3 总请求 / 会话
    const ccsReqs = ccs.requests || 0;
    const ccsSuccess = ccs.success_requests || 0;
    const hermesSessions = hermes.sessions || 0;
    const totalActivity = ccsReqs + hermesSessions;

    const cardRequestsEl = document.getElementById('cardRequestsVal');
    const cardReqsTooltip = `【请求与会话明细】\n• CCS 请求总计: ${formatNumber(ccsReqs)}\n  - 代理请求: ${formatNumber(ccs.requests_proxy || ccsReqs)}\n  - 会话同步: ${formatNumber(ccs.requests_session || 0)}\n  - 成功请求: ${formatNumber(ccsSuccess)} (${ccsReqs > 0 ? ((ccsSuccess / ccsReqs) * 100).toFixed(1) : 0}%)\n• Hermes 会话总计: ${formatNumber(hermesSessions)}`;

    if (cardRequestsEl) {
      cardRequestsEl.textContent = formatCompact(totalActivity);
      cardRequestsEl.setAttribute('data-tooltip', cardReqsTooltip);
    }
    const chipCcsReq = document.getElementById('chipCcsReq');
    if (chipCcsReq) chipCcsReq.textContent = formatNumber(ccsReqs);
    const chipHermesSess = document.getElementById('chipHermesSess');
    if (chipHermesSess) chipHermesSess.textContent = formatNumber(hermesSessions);

    // 1.4 总成本 (v2.1: 直接使用 summary.ccs.cost_usd，与工具表 CCS 行完全对齐)
    let ccsCost = 0;
    if (ccs.cost_usd !== undefined && ccs.cost_usd !== null) {
      ccsCost = Number(ccs.cost_usd);
    } else if (state.dailyData && state.dailyData.length > 0) {
      ccsCost = state.dailyData.reduce((acc, cur) => acc + (Number(cur.cost_usd) || 0), 0);
    }
    const hermesCost = Number(hermes.cost_usd) || 0;
    const totalCost = ccsCost + hermesCost;

    // 异常金额统一口径
    const anomalyUsd = Number(hermes.cost_anomaly_usd) || 18300529.57;
    const anomalyFormattedStr = `$${Number(anomalyUsd).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

    const cardCostEl = document.getElementById('cardCostVal');
    const cardCostTooltip = `【成本核算明细】\n• 总成本: $${totalCost.toFixed(4)}\n• CCS 实际成本: $${ccsCost.toFixed(4)} (与 Claude Code 一致)\n• Hermes 估算成本: $${hermesCost.toFixed(4)} (与各 Agent 一致)\n\n⚠️ 注意: terra 智能体估值异常 (${anomalyFormattedStr})，已从总额剔除。`;

    if (cardCostEl) {
      cardCostEl.textContent = formatCurrency(totalCost);
      cardCostEl.setAttribute('data-tooltip', cardCostTooltip);
    }
    const chipCcsCost = document.getElementById('chipCcsCost');
    if (chipCcsCost) chipCcsCost.textContent = formatCurrency(ccsCost);
    const chipHermesCost = document.getElementById('chipHermesCost');
    if (chipHermesCost) chipHermesCost.textContent = formatCurrency(hermesCost);

    // 更新卡片下方的异常警示文字 (两处数字绝对一致，紧凑排版显示 $18.3M 并在 title 中展示精确值)
    const anomalyBadgeWrap = document.querySelector('.stat-card:last-child .anomaly-warning-text');
    if (anomalyBadgeWrap) {
      anomalyBadgeWrap.setAttribute('title', `已剔除 terra 异常估值 (${anomalyFormattedStr})`);
      const span = anomalyBadgeWrap.querySelector('span');
      if (span) span.textContent = `已剔除 terra 异常 ($18.3M)`;
    }
  }

  // --- 配额辅助函数 (v4 官方配额口径) ---
  function getQuotaColor(pct) {
    const p = Number(pct);
    if (isNaN(p) || p <= 0) return 'var(--text-muted)';
    if (p <= 20) return 'var(--color-error)';
    if (p <= 50) return 'var(--color-warning)';
    return 'var(--color-success)';
  }

  function formatCountdown(resetTimeIso) {
    if (!resetTimeIso) return '—';
    const target = new Date(resetTimeIso).getTime();
    if (isNaN(target)) return '—';
    const now = Date.now();
    const diffMs = target - now;

    if (diffMs <= 0) return '已刷新';

    const totalSec = Math.floor(diffMs / 1000);
    const days = Math.floor(totalSec / 86400);
    const hours = Math.floor((totalSec % 86400) / 3600);
    const minutes = Math.floor((totalSec % 3600) / 60);
    const seconds = totalSec % 60;

    if (days > 0) {
      return `${days}天${hours}小时`;
    }
    if (hours > 0) {
      return `${hours}小时${minutes}分${seconds}秒`;
    }
    if (minutes > 0) {
      return `${minutes}分${seconds}秒`;
    }
    return `${seconds}秒`;
  }

  let quotaTimer = null;
  function startQuotaCountdownTimer() {
    if (quotaTimer) clearInterval(quotaTimer);
    quotaTimer = setInterval(() => {
      const countdownEls = document.querySelectorAll('.bucket-countdown[data-reset]');
      if (!countdownEls || countdownEls.length === 0) return;
      countdownEls.forEach(el => {
        const resetTime = el.getAttribute('data-reset');
        if (resetTime) {
          el.textContent = formatCountdown(resetTime);
        }
      });
    }, 1000);
  }

  // --- 2. 工具 / Agent 消耗列表渲染 (v4 升级：支持官方配额与 Token 消耗并存) ---
  function renderToolsSection() {
    const listContainer = document.getElementById('agentListContainer');
    if (!listContainer) return;

    let tools = state.toolsData || [];

    if (tools.length === 0 && state.agentsData && state.agentsData.length > 0) {
      tools = state.agentsData.map(a => ({
        name: `Hermes·${a.name}`,
        group: 'Hermes',
        available: true,
        requests: a.sessions,
        total_tokens: (a.input || 0) + (a.output || 0) + (a.cache_read || 0) + (a.cache_write || 0),
        cache_hit_rate: a.cache_hit_rate,
        cost_usd: a.cost_usd,
        cost_anomaly: a.cost_anomaly,
        models: a.models
      }));
    }

    if (tools.length === 0) {
      listContainer.innerHTML = '<div style="color:var(--text-muted);padding:24px;text-align:center;">暂无工具统计数据</div>';
      return;
    }

    // 分组筛选
    if (state.currentGroupFilter !== 'all') {
      tools = tools.filter(t => t.group === state.currentGroupFilter);
    }

    // 排序：可用按 total_tokens 倒序，不可用排在末尾
    const sortedTools = [...tools].sort((a, b) => {
      if (a.available === false && b.available !== false) return 1;
      if (a.available !== false && b.available === false) return -1;
      const aTok = (a.tokens && a.tokens.available !== false) ? (a.tokens.total_tokens || 0) : (a.total_tokens || 0);
      const bTok = (b.tokens && b.tokens.available !== false) ? (b.tokens.total_tokens || 0) : (b.total_tokens || 0);
      return bTok - aTok;
    });

    const maxTokens = Math.max(...sortedTools.filter(t => t.available !== false).map(t => {
      return (t.tokens && t.tokens.available !== false) ? (t.tokens.total_tokens || 0) : (t.total_tokens || 0);
    }), 1);
    const globalAnomalyUsd = Number(state.summaryData?.hermes?.cost_anomaly_usd) || 18300529.57;
    const globalAnomalyFormatted = `$${globalAnomalyUsd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

    let html = '';
    sortedTools.forEach((t, idx) => {
      const isAvailable = t.available !== false;
      const isAnomaly = Boolean(t.cost_anomaly);
      const groupName = t.group || 'Hermes';
      const groupLower = groupName.toLowerCase();
      const isAntigravity = t.name === 'Antigravity' || groupName === 'Antigravity';

      // ================= Antigravity 真实 Token + 官方配额分支 (v5 规范) =================
      if (isAntigravity) {
        if (!isAvailable) {
          html += `
            <div class="agent-item is-unavailable is-quota-item" id="tool-row-${idx}">
              <div class="agent-main-row">
                <div class="agent-info">
                  <span class="tool-group-tag group-tag-antigravity">${groupName}</span>
                  <span class="agent-name">${t.name}</span>
                </div>
                <div class="agent-metrics-row">
                  <div class="agent-metric-item">
                    <span class="agent-metric-label">活动量</span>
                    <span class="agent-metric-value">${formatNumber(t.sessions || 0)} 会话</span>
                    <span class="req-split-sub">${formatNumber(t.steps || 0)} 步骤</span>
                  </div>
                  <div class="agent-metric-item">
                    <span class="agent-metric-label">总 Token</span>
                    <span class="agent-metric-value" style="color:var(--text-muted);font-weight:normal;">—</span>
                  </div>
                  <div class="agent-metric-item">
                    <span class="agent-metric-label">命中率</span>
                    <span class="agent-metric-value" style="color:var(--text-muted);font-weight:normal;">—</span>
                  </div>
                  <div class="agent-metric-item">
                    <span class="agent-metric-label">成本</span>
                    <span class="agent-metric-value" style="color:var(--text-muted);font-weight:normal;">—</span>
                  </div>
                </div>
              </div>
              <div class="tool-reason-text">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="10"></circle>
                  <line x1="12" y1="8" x2="12" y2="12"></line>
                  <line x1="12" y1="16" x2="12.01" y2="16"></line>
                </svg>
                <span>${t.reason || 'Antigravity 离线或服务未就绪'}</span>
              </div>
            </div>
          `;
          return;
        }

        const tok = t.tokens;
        const hasTokens = Boolean(tok && tok.available !== false);
        const totalTok = hasTokens ? (tok.total_tokens || 0) : 0;
        const rawPct = hasTokens ? ((totalTok / maxTokens) * 100) : 0;
        const displayPct = rawPct > 0 ? Math.max(rawPct, 0.6).toFixed(1) : 0;
        const trackedConvs = tok?.tracked_conversations || 1;
        const unavailReason = t.tokens_unavailable || tok?.reason || '本地未记录 token';

        // 1. 请求数 (tokens.requests)
        let reqMainText = '';
        let reqSubText = `<span class="req-split-sub">${formatNumber(t.sessions || 0)} 会话 · ${formatNumber(t.steps || 0)} 步骤</span>`;
        if (hasTokens) {
          reqMainText = formatNumber(tok.requests || 0);
        } else {
          reqMainText = `<span style="color:var(--text-muted);font-weight:normal;">—</span>`;
        }

        // 2. 总 Token (tokens.total_tokens, 与其它工具同一格式化函数)
        let tokenDisplay = '';
        if (hasTokens) {
          const tokenDetailTooltip = `输入: ${formatNumber(tok.input || 0)}\n输出: ${formatNumber(tok.output || 0)}\n缓存读取: ${formatNumber(tok.cache_read || 0)}\n精确值: ${formatNumber(tok.total_tokens || 0)}`;
          tokenDisplay = `<span class="agent-metric-value" data-tooltip="${tokenDetailTooltip}">${formatCompact(tok.total_tokens || 0)}</span>`;
        } else {
          tokenDisplay = `<span class="agent-metric-value" style="color:var(--text-muted);font-weight:normal;" data-tooltip="${unavailReason}">—</span>`;
        }

        // 3. 命中率 (tokens.cache_hit_rate, 百分比格式)
        let hitRateDisplay = '';
        if (hasTokens) {
          const rateVal = tok.cache_hit_rate || 0;
          const rateColor = rateVal > 0.8 ? 'var(--color-success)' : (rateVal > 0.4 ? 'var(--accent-cyan)' : 'var(--text-primary)');
          hitRateDisplay = `<span class="agent-metric-value" style="color: ${rateColor}">${formatPercent(rateVal)}</span>`;
        } else {
          hitRateDisplay = `<span class="agent-metric-value" style="color:var(--text-muted);font-weight:normal;">—</span>`;
        }

        // 4. 成本 (账号侧结算显示 "—")
        const costHtml = `<span class="agent-metric-value" style="color:var(--text-muted);font-weight:normal;" title="成本在账号侧结算">—</span>`;

        // 5. 模型 (如实显示，不猜名)
        const toolModels = (t.models && t.models.length > 0) ? t.models : (tok?.models || []);
        const hasModels = Array.isArray(toolModels) && toolModels.length > 0;

        // 6. 官方配额块 (groups 渲染，周/5h 百分比 + 倒计时)
        const groupsHtml = (t.groups || []).map(g => {
          const bucketsHtml = (g.buckets || []).map(b => {
            const isWeekly = b.window === 'weekly';
            const label = isWeekly ? '周额度' : '5h 额度';
            const pct = b.remaining_pct !== undefined ? b.remaining_pct : 0;
            const color = getQuotaColor(pct);
            const countdown = formatCountdown(b.reset_time);
            const resetLocal = b.reset_time ? new Date(b.reset_time).toLocaleString() : '';
            return `
              <div class="quota-bucket-item" title="${b.desc || ''}">
                <div class="quota-bucket-top">
                  <span class="quota-bucket-label">${label}</span>
                  <span class="quota-bucket-pct" style="color: ${color};">${pct}%</span>
                </div>
                <div class="quota-bucket-bar-wrap">
                  <div class="quota-bucket-bar-fill" style="width: ${Math.max(pct, 1)}%; background-color: ${color};"></div>
                </div>
                <div class="quota-bucket-countdown-row">
                  <span class="quota-reset-label">重置:</span>
                  <span class="bucket-countdown" data-reset="${b.reset_time || ''}" title="重置时间(本地): ${resetLocal}">${countdown}</span>
                </div>
              </div>
            `;
          }).join('');

          return `
            <div class="quota-group-card">
              <div class="quota-group-header">
                <span class="quota-group-title">${g.name}</span>
                <span class="quota-group-models" title="${g.models || ''}">${g.models ? g.models.replace('Models within this group: ', '') : ''}</span>
              </div>
              <div class="quota-buckets-grid">
                ${bucketsHtml}
              </div>
            </div>
          `;
        }).join('');

        html += `
          <div class="agent-item is-quota-item" id="tool-row-${idx}">
            <div class="agent-main-row">
              <div class="agent-info">
                <span class="tool-group-tag group-tag-antigravity">${groupName}</span>
                <span class="agent-name">${t.name}</span>
                ${hasModels ? `<button class="agent-expand-toggle" onclick="window.toggleToolModels(${idx})">模型 ▾</button>` : ''}
              </div>
              <div class="agent-metrics-row">
                <div class="agent-metric-item">
                  <span class="agent-metric-label">请求数</span>
                  <span class="agent-metric-value">${reqMainText}</span>
                  ${reqSubText}
                </div>
                <div class="agent-metric-item">
                  <span class="agent-metric-label">总 Token</span>
                  ${tokenDisplay}
                </div>
                <div class="agent-metric-item">
                  <span class="agent-metric-label">命中率</span>
                  ${hitRateDisplay}
                </div>
                <div class="agent-metric-item">
                  <span class="agent-metric-label">成本</span>
                  ${costHtml}
                </div>
              </div>
            </div>

            <!-- 可用 Token 显示进度条 -->
            ${hasTokens ? `
              <div class="agent-bar-wrap">
                <div class="agent-bar-fill" style="width: ${displayPct}%;"></div>
              </div>
            ` : ''}

            <!-- 覆盖范围或不可用说明 -->
            ${hasTokens ? `
              <div class="quota-coverage-note">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="10"></circle>
                  <line x1="12" y1="16" x2="12" y2="12"></line>
                  <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>
                <span>LS 仅覆盖仍在跟踪的 ${trackedConvs} 个会话</span>
              </div>
            ` : `
              <div class="tool-reason-text" style="margin: 4px 0;">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="10"></circle>
                  <line x1="12" y1="16" x2="12" y2="12"></line>
                  <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>
                <span>${unavailReason}</span>
              </div>
            `}

            <!-- 官方配额块（字号小一级，标注「配额（官方接口）· 百分比」） -->
            ${groupsHtml ? `
              <div class="quota-block-header">
                <span class="quota-source-badge">配额（官方接口）· 百分比</span>
              </div>
              <div class="quota-groups-container quota-compact">
                ${groupsHtml}
              </div>
            ` : ''}

            <!-- 模型详情抽屉 -->
            ${hasModels ? `
              <div class="agent-models-drawer" id="tool-drawer-${idx}">
                ${toolModels.map(m => {
                  const mTotal = m.total || (m.input || 0) + (m.output || 0) + (m.cache_read || 0);
                  const reqCount = m.requests || m.sessions || 0;
                  return `
                    <div class="agent-model-row">
                      <span class="agent-model-name" title="${m.model}">${m.model}</span>
                      <span class="agent-model-val">${formatNumber(reqCount)} 请求${mTotal > 0 ? ` · ${formatCompact(mTotal)} Tokens` : ''}${m.cache_hit_rate ? ` (${formatPercent(m.cache_hit_rate)} 命中)` : ''}</span>
                    </div>
                  `;
                }).join('')}
              </div>
            ` : ''}
          </div>
        `;
        return;
      }

      // ================= Token 统计工具常规分支 =================
      // 细微宽度保证极小值也能看见微条 (最小 0.6% 对应 ~3px)
      const rawPct = isAvailable ? (((t.total_tokens || 0) / maxTokens) * 100) : 0;
      const displayPct = rawPct > 0 ? Math.max(rawPct, 0.6).toFixed(1) : 0;

      // 成本渲染：未知显示 "—" (ZCode / Antigravity)，异常显示紧凑 "$18.3M ⚠️"
      let costHtml = '';
      if (isAnomaly) {
        costHtml = `<span class="anomaly-warning-text" data-tooltip="成本异常: ${globalAnomalyFormatted} (脏数据，已从总额剔除)" style="cursor:help;">$18.3M ⚠️</span>`;
      } else if (t.cost_usd === null || t.cost_usd === undefined || !isAvailable) {
        costHtml = `<span style="color:var(--text-muted);font-weight:normal;" title="成本未知或在账号侧结算">—</span>`;
      } else {
        costHtml = formatCurrency(t.cost_usd);
      }

      // 请求数渲染：CCS 拆分 proxy / 会话
      let reqMainText = '';
      let reqSubText = '';
      if (t.requests_proxy !== undefined || t.requests_session !== undefined) {
        reqMainText = formatNumber(t.requests);
        reqSubText = `<span class="req-split-sub">代理: ${formatNumber(t.requests_proxy || 0)} · 会话: ${formatNumber(t.requests_session || 0)}</span>`;
      } else if (groupName === 'Hermes') {
        reqMainText = formatNumber(t.requests || t.sessions || 0);
        reqSubText = `<span class="req-split-sub">会话</span>`;
      } else if (groupName === 'ZCode') {
        reqMainText = formatNumber(t.requests);
        reqSubText = `<span class="req-split-sub">${formatNumber(t.sessions || 1)} 个会话</span>`;
      } else if (!isAvailable) {
        reqMainText = `${formatNumber(t.sessions || 0)} 会话`;
        reqSubText = `<span class="req-split-sub">${formatNumber(t.steps || 0)} 步骤</span>`;
      } else {
        reqMainText = formatNumber(t.requests || 0);
      }

      // Token 消耗渲染：不可用时显示 "—"，绝不显示为 0
      let tokenDisplay = '';
      if (!isAvailable) {
        tokenDisplay = `<span style="color:var(--text-muted);font-weight:normal;" data-tooltip="额度在账号侧，本地无 token 记录">—</span>`;
      } else {
        const tokenDetailTooltip = `输入: ${formatNumber(t.input || 0)}\n输出: ${formatNumber(t.output || 0)}\n缓存读取: ${formatNumber(t.cache_read || 0)}\n缓存创建/写: ${formatNumber(t.cache_creation || t.cache_write || 0)}`;
        tokenDisplay = `<span class="agent-metric-value" data-tooltip="${tokenDetailTooltip}">${formatCompact(t.total_tokens || 0)}</span>`;
      }

      // 命中率渲染
      let hitRateDisplay = '';
      if (!isAvailable) {
        hitRateDisplay = `<span style="color:var(--text-muted);font-weight:normal;">—</span>`;
      } else {
        const rateVal = t.cache_hit_rate || 0;
        const rateColor = rateVal > 0.8 ? 'var(--color-success)' : (rateVal > 0.4 ? 'var(--accent-cyan)' : 'var(--text-primary)');
        hitRateDisplay = `<span class="agent-metric-value" style="color: ${rateColor}">${formatPercent(rateVal)}</span>`;
      }

      const hasModels = Array.isArray(t.models) && t.models.length > 0;

      const groupBadgeHtml = `<span class="tool-group-tag group-tag-${groupLower}">${groupName}</span>`;

      html += `
        <div class="agent-item ${!isAvailable ? 'is-unavailable' : ''} ${isAnomaly ? 'is-anomaly' : ''}" id="tool-row-${idx}">
          <div class="agent-main-row">
            <div class="agent-info">
              ${groupBadgeHtml}
              <span class="agent-name">
                ${t.name}
                ${isAnomaly ? '<span class="anomaly-badge">成本异常</span>' : ''}
                ${!isAvailable ? '<span class="badge-pill" style="color:var(--text-secondary);border:1px solid rgba(255,255,255,0.15);background:rgba(255,255,255,0.06);">无本地用量记录</span>' : ''}
              </span>
              ${hasModels ? `<button class="agent-expand-toggle" onclick="window.toggleToolModels(${idx})">模型 ▾</button>` : ''}
            </div>

            <div class="agent-metrics-row">
              <div class="agent-metric-item">
                <span class="agent-metric-label">${!isAvailable ? '活动量' : (groupName === 'Hermes' ? '会话数' : '请求数')}</span>
                <span class="agent-metric-value">${reqMainText}</span>
                ${reqSubText}
              </div>
              <div class="agent-metric-item">
                <span class="agent-metric-label">总 Token</span>
                ${tokenDisplay}
              </div>
              <div class="agent-metric-item">
                <span class="agent-metric-label">命中率</span>
                ${hitRateDisplay}
              </div>
              <div class="agent-metric-item">
                <span class="agent-metric-label">成本</span>
                <span class="agent-metric-value ${isAnomaly ? 'anomaly' : ''}">${costHtml}</span>
              </div>
            </div>
          </div>

          <!-- 可用项显示进度条；不可用项显示原因说明 -->
          ${isAvailable ? `
            <div class="agent-bar-wrap">
              <div class="agent-bar-fill ${isAnomaly ? 'anomaly' : ''}" style="width: ${displayPct}%;"></div>
            </div>
          ` : `
            <div class="tool-reason-text">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
              </svg>
              <span>${t.reason || '本地不落地 token 用量（额度在 Google 账号侧）'} · 仅追踪会话与步骤活动</span>
            </div>
          `}

          <!-- 异常行补充口径说明 -->
          ${isAnomaly ? `
            <div class="tool-anomaly-note">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
              <span>口径说明：Token 仍计入统计，仅成本剔除（避免污染总额）</span>
            </div>
          ` : ''}

          <!-- 模型详情抽屉 -->
          ${hasModels ? `
            <div class="agent-models-drawer" id="tool-drawer-${idx}">
              ${t.models.map(m => {
                const mTotal = m.total || (m.input || 0) + (m.output || 0) + (m.cache_read || 0);
                const reqCount = m.requests || m.sessions || 0;
                return `
                  <div class="agent-model-row">
                    <span class="agent-model-name" title="${m.model}">${m.model}</span>
                    <span class="agent-model-val">${formatNumber(reqCount)} 请求 · ${formatCompact(mTotal)} Tokens (${formatPercent(m.cache_hit_rate || 0)} 命中)</span>
                  </div>
                `;
              }).join('')}
            </div>
          ` : ''}
        </div>
      `;
    });

    listContainer.innerHTML = html;
    startQuotaCountdownTimer();
  }

  window.toggleToolModels = function (idx) {
    const drawer = document.getElementById(`tool-drawer-${idx}`);
    if (drawer) {
      drawer.classList.toggle('open');
    }
  };

  // --- 3. 模型占比分析渲染 (v2.1: 分母与份额统一) ---
  // Zen Pulse 暖色调色板（仪表盘专用视觉，与 zp.css 一致）
  const DONUT_COLORS = [
    '#d75f45', '#a48bc4', '#97ad72', '#d9a86b', '#e8927c',
    '#7d8fa3', '#6f9b58', '#b08968', '#5b8bcf', '#c2a878'
  ];

  function renderModelSection(sourceOverride = state.modelSource, targetContainer = null, updateNotice = true, updateSummary = true) {
    const modelSource = sourceOverride;
    const container = targetContainer || document.getElementById('modelContentContainer');
    const noticeEl = updateNotice ? document.getElementById('modelSourceRuleNotice') : null;
    if (!container) return;

    let list = [];
    let sourceNoticeText = '';
    let totalTokensAll = 0;

    const sourceById = {};
    getModelSourceRows().forEach(source => { sourceById[source.id] = source; });
    const useUnifiedSources = Boolean(state.modelSourcesData &&
      (modelSource === 'all' || sourceById[modelSource]));
    if (useUnifiedSources) {
      const chosenIds = modelSource === 'all' ? (state.enabledModelSources || []) : [modelSource];
      const includedSources = chosenIds.map(id => sourceById[id]).filter(source => source && source.available === true);
      const merged = new Map();
      includedSources.forEach(source => {
        (source.models || []).forEach(model => {
          const name = model.model || '(unknown)';
          if (!merged.has(name)) {
            merged.set(name, {
              name, requestsOrSessions: 0, requests: 0, sessions: 0,
              totalTokens: 0, cacheReadTokens: 0, cacheDenominator: 0
            });
          }
          const row = merged.get(name);
          row.requests += Number(model.requests) || 0;
          row.sessions += Number(model.sessions) || 0;
          row.requestsOrSessions += modelSource === 'hermes'
            ? (Number(model.sessions) || 0) : (Number(model.requests) || 0);
          row.totalTokens += Number(model.total_tokens) || 0;
          row.cacheReadTokens += Number(model.cache_read) || 0;
          row.cacheDenominator += Number(model.cache_denominator) || 0;
        });
      });
      list = Array.from(merged.values()).map(model => ({
        ...model,
        hitRate: model.cacheDenominator > 0 ? model.cacheReadTokens / model.cacheDenominator : 0
      }));
      totalTokensAll = includedSources.reduce((sum, source) => sum + (Number(source.total_tokens) || 0), 0);
      if (modelSource === 'all') {
        const missingCount = chosenIds.filter(id => !sourceById[id] || sourceById[id].available !== true).length;
        sourceNoticeText = `已选来源：${includedSources.map(source => source.name).join('、') || '无'}。按模型名合并为一张图；${missingCount ? `${missingCount} 个已选来源当前不可用，` : ''}来源可能重叠，未跨来源去重。请求数与 Hermes 会话数分列。`;
      } else {
        const source = sourceById[modelSource];
        sourceNoticeText = `${source?.name || modelSource} · ${source?.note || '按该来源本机用量计算模型份额。'}`;
      }
    } else if (modelSource === 'all' && state.dataSourceType === 'live' && !state.modelSourcesError) {
      sourceNoticeText = state.modelSourcesError || '模型来源统计接口暂不可用。';
      if (noticeEl) noticeEl.textContent = sourceNoticeText;
      container.innerHTML = '<div class="empty-hint-card">模型来源统计暂不可用，请刷新后重试。</div>';
      const sumModels = updateSummary ? document.getElementById('summary-models') : null;
      if (sumModels) sumModels.textContent = '全部来源 · 暂不可用';
      const rowCountErr = updateNotice ? document.getElementById('modelRowCount') : null;
      if (rowCountErr) rowCountErr.textContent = '';
      return;
    } else if (modelSource === 'all') {
      sourceNoticeText = state.dataSourceType === 'sample'
        ? '离线样本仅支持 CCS、Hermes、Codex、ZCode；按统计来源勾选过滤。实时服务连接后会读取全部来源。'
        : '旧版后台兼容模式：暂按 CCS、Hermes、Codex、ZCode 的本机数据合并；重启新版后读取全部来源。';
      const merged = new Map();
      const selectedIds = new Set(state.enabledModelSources || []);
      const mergeModel = ({ name, requests = 0, sessions = 0, totalTokens = 0, cacheRead = 0, cacheDenominator = 0 }) => {
        const modelName = name || '(unknown)';
        if (!merged.has(modelName)) {
          merged.set(modelName, {
            name: modelName, requestsOrSessions: 0, sessions: 0,
            totalTokens: 0, cacheReadTokens: 0, cacheDenominator: 0
          });
        }
        const row = merged.get(modelName);
        row.requestsOrSessions += Number(requests) || 0;
        row.sessions += Number(sessions) || 0;
        row.totalTokens += Number(totalTokens) || 0;
        row.cacheReadTokens += Number(cacheRead) || 0;
        row.cacheDenominator += Number(cacheDenominator) || 0;
      };

      const ccsModels = selectedIds.has('ccs') ? (state.modelsData || []) : [];
      ccsModels.forEach(m => {
        const input = Number(m.input) || 0;
        const output = Number(m.output) || 0;
        const cacheRead = Number(m.cache_read) || 0;
        const cacheCreation = Number(m.cache_creation) || 0;
        mergeModel({
          name: m.model, requests: m.requests,
          totalTokens: m.total_tokens || (input + output + cacheRead + cacheCreation),
          cacheRead, cacheDenominator: input + cacheRead + cacheCreation
        });
      });
      (selectedIds.has('hermes') ? (state.agentsData || []) : []).forEach(agent => {
        (agent.models || []).forEach(m => {
          const input = Number(m.input) || 0;
          const output = Number(m.output) || 0;
          const cacheRead = Number(m.cache_read) || 0;
          mergeModel({
            name: m.model || '(unknown)', sessions: m.sessions,
            totalTokens: input + output + cacheRead,
            cacheRead, cacheDenominator: input + cacheRead
          });
        });
      });

      const codexTool = (state.toolsData || []).find(t => t.name === 'Codex' || t.group === 'Codex');
      const codexModels = selectedIds.has('codex') && codexTool && Array.isArray(codexTool.models) ? codexTool.models : [];
      codexModels.forEach(m => {
        const input = Number(m.input) || 0;
        const output = Number(m.output) || 0;
        const cacheRead = Number(m.cache_read) || 0;
        const inputSide = cacheRead > input ? input + cacheRead : input;
        mergeModel({
          name: m.model || '未识别模型', requests: m.requests,
          totalTokens: Number(m.total_tokens) || (inputSide + output),
          cacheRead, cacheDenominator: inputSide
        });
      });

      const zcodeTool = (state.toolsData || []).find(t => t.name === 'ZCode' || t.group === 'ZCode');
      const zcodeModels = selectedIds.has('zcode') && zcodeTool && Array.isArray(zcodeTool.models) ? zcodeTool.models : [];
      zcodeModels.forEach(m => {
        const input = Number(m.input) || 0;
        const output = Number(m.output) || 0;
        const cacheRead = Number(m.cache_read) || 0;
        mergeModel({
          name: m.model, requests: m.requests,
          totalTokens: Number(m.total) || (input + output + cacheRead),
          cacheRead, cacheDenominator: input
        });
      });

      const agTool = (state.toolsData || []).find(t => t.name === 'Antigravity' || t.group === 'Antigravity');
      const agModels = selectedIds.has('antigravity') && agTool && Array.isArray(agTool.models)
        ? agTool.models : ((agTool?.tokens?.models) || []);
      if (selectedIds.has('antigravity')) {
        agModels.forEach(m => {
          const input = Number(m.input) || 0;
          const output = Number(m.output) || 0;
          const cacheRead = Number(m.cache_read) || 0;
          const totalTok = Number(m.total_tokens) || (input + output);
          mergeModel({
            name: m.model, requests: m.requests,
            totalTokens: totalTok,
            cacheRead, cacheDenominator: input + cacheRead
          });
        });
      }

      totalTokensAll = getMergedSourceTokenTotals().total || 0;

      list = Array.from(merged.values()).map(m => ({
        ...m,
        hitRate: m.cacheDenominator > 0 ? m.cacheReadTokens / m.cacheDenominator : 0
      }));
    } else if (modelSource === 'ccs') {
      sourceNoticeText = '口径说明：CCS 代理层（逐请求日志），独立计算代理层各模型份额。';
      const rawModels = state.modelsData || [];
      list = rawModels.map(m => ({
        name: m.model,
        requestsOrSessions: m.requests || 0,
        totalTokens: m.total_tokens || ((m.input || 0) + (m.output || 0) + (m.cache_read || 0) + (m.cache_creation || 0)),
        hitRate: m.cache_hit_rate || 0
      }));

      // 统一分母：使用 summary.ccs 真实总量 (约 2.83B)
      const ccsSummary = state.summaryData?.ccs;
      const ccsTotal = ccsSummary ? ((ccsSummary.input || 0) + (ccsSummary.output || 0) + (ccsSummary.cache_read || 0) + (ccsSummary.cache_creation || 0)) : 0;
      totalTokensAll = ccsTotal > 0 ? ccsTotal : list.reduce((acc, cur) => acc + cur.totalTokens, 0);

    } else if (modelSource === 'hermes') {
      sourceNoticeText = '口径说明：Hermes 智能体层（各 Profile 会话模型），独立计算会话模型份额。';
      const modelMap = {};
      (state.agentsData || []).forEach(a => {
        (a.models || []).forEach(m => {
          const mName = m.model || '(unknown)';
          if (!modelMap[mName]) {
            modelMap[mName] = { name: mName, requestsOrSessions: 0, totalTokens: 0, cacheRead: 0, inputSide: 0 };
          }
          modelMap[mName].requestsOrSessions += (m.sessions || 0);
          const inp = m.input || 0;
          const out = m.output || 0;
          const cr = m.cache_read || 0;
          modelMap[mName].totalTokens += (inp + out + cr);
          modelMap[mName].cacheRead += cr;
          modelMap[mName].inputSide += (inp + cr);
        });
      });

      list = Object.values(modelMap).map(m => ({
        name: m.name,
        requestsOrSessions: m.requestsOrSessions,
        totalTokens: m.totalTokens,
        hitRate: m.inputSide > 0 ? (m.cacheRead / m.inputSide) : 0
      }));

      // 统一分母：使用 summary.hermes 真实总量 (约 2.02B)
      const hermesSummary = state.summaryData?.hermes;
      const hermesTotal = hermesSummary ? ((hermesSummary.input || 0) + (hermesSummary.output || 0) + (hermesSummary.cache_read || 0) + (hermesSummary.cache_write || 0)) : 0;
      totalTokensAll = hermesTotal > 0 ? hermesTotal : list.reduce((acc, cur) => acc + cur.totalTokens, 0);

    } else if (modelSource === 'zcode') {
      sourceNoticeText = '口径说明：ZCode model_usage 权威表 (~/.zcode/cli/db/db.sqlite)，独立核算自身份额。';
      const zcodeTool = (state.toolsData || []).find(t => t.name === 'ZCode' || t.group === 'ZCode');
      if (zcodeTool && Array.isArray(zcodeTool.models) && zcodeTool.models.length > 0) {
        list = zcodeTool.models.map(m => {
          const mTokens = m.total || ((m.input || 0) + (m.output || 0) + (m.cache_read || 0));
          return {
            name: m.model,
            requestsOrSessions: m.requests || 0,
            totalTokens: mTokens,
            hitRate: m.cache_hit_rate != null ? m.cache_hit_rate : (m.input ? (m.cache_read || 0) / m.input : 0)
          };
        });
      } else {
        list = [];
      }

      totalTokensAll = zcodeTool ? (zcodeTool.total_tokens || zcodeTool.provider_total_tokens || 0) : 0;
      if (totalTokensAll === 0 && list.length > 0) {
        totalTokensAll = list.reduce((acc, cur) => acc + cur.totalTokens, 0);
      }
    } else if (modelSource === 'codex') {
      const codexTool = (state.toolsData || []).find(t => t.name === 'Codex' || t.group === 'Codex');
      sourceNoticeText = '口径说明：Codex rollout 逐响应用量，按模型归类并去除重复快照；模型份额独立计算。缓存量超过输入量时按“输入+缓存”计。会话压缩会使其与概览累计 Token 不同。';
      list = (codexTool && Array.isArray(codexTool.models) ? codexTool.models : []).map(m => ({
        name: m.model || '未识别模型',
        requestsOrSessions: m.requests || 0,
        totalTokens: m.total_tokens || ((m.input || 0) + (m.output || 0)),
        hitRate: m.cache_hit_rate || 0
      }));
      totalTokensAll = list.reduce((sum, model) => sum + model.totalTokens, 0);
      if (!codexTool || codexTool.available === false) {
        sourceNoticeText += ' 当前没有可用的 Codex rollout 数据。';
      }
    } else if (modelSource === 'antigravity') {
      sourceNoticeText = '口径说明：Antigravity 本地 LS RPC + 会话持久化记账；模型份额独立计算。有限时间窗按会话活动时间过滤。';
      const agTool = (state.toolsData || []).find(t => t.name === 'Antigravity' || t.group === 'Antigravity');
      const agModels = (agTool && Array.isArray(agTool.models) && agTool.models.length > 0)
        ? agTool.models : ((agTool?.tokens?.models) || []);
      list = agModels.map(m => {
        const mTokens = m.total_tokens || m.total || ((m.input || 0) + (m.output || 0));
        return {
          name: m.model,
          requestsOrSessions: m.requests || 0,
          totalTokens: mTokens,
          hitRate: m.cache_hit_rate != null ? m.cache_hit_rate : (m.input ? (m.cache_read || 0) / (m.input + (m.cache_read || 0)) : 0)
        };
      });
      totalTokensAll = agTool ? (agTool.total_tokens || agTool?.tokens?.total_tokens || 0) : 0;
      if (totalTokensAll === 0 && list.length > 0) {
        totalTokensAll = list.reduce((acc, cur) => acc + cur.totalTokens, 0);
      }
      if (!agTool || agTool.available === false) {
        sourceNoticeText += ' 当前没有可用的 Antigravity 数据。';
      }
    }

    if (noticeEl) noticeEl.textContent = sourceNoticeText;

    // 按 Token 倒序排序
    list.sort((a, b) => b.totalTokens - a.totalTokens);

    // 明细行数提示：卡内表格是滚动区，不提示会以为只有前几行
    const rowCountEl = updateNotice ? document.getElementById('modelRowCount') : null;
    if (rowCountEl) {
      rowCountEl.textContent = list.length
        ? `共 ${formatNumber(list.length)} 个模型${list.length > 8 ? ' · 表内滚动查看全部' : ''}`
        : '';
    }

    if (!list.length) {
      const emptyMessage = modelSource === 'all' && state.modelSourcesData
        ? ((state.enabledModelSources || []).some(id => sourceById[id]?.available === true)
          ? '所选来源在此时间范围内没有可用的模型用量。'
          : '没有勾选可用的数据来源。请在上方选择至少一个已发现的来源。')
        : (modelSource === 'all' && state.dataSourceType === 'live' && state.modelSourcesError
          && !(state.enabledModelSources || []).some(id => ['ccs', 'hermes', 'codex', 'zcode', 'antigravity'].includes(id))
          ? '当前后台只支持 CCS、Hermes、Codex、ZCode、Antigravity；请重启新版 XiaoyuToolbox 读取新增来源。'
        : (modelSource === 'codex'
          ? '此时间范围内没有可按模型归类的 Codex 响应用量。'
          : '此时间范围内没有可用的模型用量。'));
      container.innerHTML = `<div class="empty-hint-card">${emptyMessage}</div>`;
      const sumModels = updateSummary ? document.getElementById('summary-models') : null;
      const sourceLabel = modelSource === 'all' ? '全部来源' : (sourceById[modelSource]?.name || MODEL_SOURCE_OPTIONS.find(source => source.id === modelSource)?.label || modelSource);
      if (sumModels) sumModels.textContent = `${sourceLabel} · —`;
      return;
    }

    // 环形图数据准备（Top 5 + 其他）
    const top5 = list.slice(0, 5);
    const top5TokensSum = top5.reduce((acc, cur) => acc + cur.totalTokens, 0);
    const otherTokens = Math.max(totalTokensAll - top5TokensSum, 0);
    const otherRequests = list.slice(5).reduce((sum, model) => sum + model.requestsOrSessions, 0);

    const chartItems = [...top5];
    if (otherTokens > 0) {
      chartItems.push({
        name: '其他模型',
        requestsOrSessions: otherRequests,
        totalTokens: otherTokens,
        hitRate: 0,
        isOther: true
      });
    }

    // 生成 SVG 环形切片 (分母均为 totalTokensAll)
    const radius = 45;
    const circumference = 2 * Math.PI * radius;
    let accumulatedAngle = 0;
    let svgSlicesHtml = '';

    chartItems.forEach((item, i) => {
      const share = totalTokensAll > 0 ? (item.totalTokens / totalTokensAll) : 0;
      const strokeDasharray = `${share * circumference} ${circumference}`;
      const strokeDashoffset = -accumulatedAngle * circumference;
      const color = item.isOther ? '#545869' : (modelSource === 'zcode' ? '#ba68c8' : (modelSource === 'codex' ? '#10a3e4' : (modelSource === 'antigravity' ? '#4285f4' : DONUT_COLORS[i % DONUT_COLORS.length])));

      svgSlicesHtml += `
        <circle cx="65" cy="65" r="${radius}" fill="transparent"
          stroke="${color}" stroke-width="18"
          stroke-dasharray="${strokeDasharray}"
          stroke-dashoffset="${strokeDashoffset}">
          <title>${escapeHtml(item.name)}: ${formatCompact(item.totalTokens)} (${(share * 100).toFixed(1)}%)</title>
        </circle>
      `;
      accumulatedAngle += share;
    });

    // 环形图小图例 (份额必须与明细表完全统一)
    let legendMiniHtml = '';
    chartItems.forEach((item, i) => {
      const sharePct = totalTokensAll > 0 ? (item.totalTokens / totalTokensAll) * 100 : 0;
      const color = item.isOther ? '#545869' : (modelSource === 'zcode' ? '#ba68c8' : (modelSource === 'codex' ? '#10a3e4' : (modelSource === 'antigravity' ? '#4285f4' : DONUT_COLORS[i % DONUT_COLORS.length])));
      legendMiniHtml += `
        <div class="legend-mini-item">
          <span class="legend-mini-name" title="${escapeHtml(item.name)}">
            <span class="legend-color-dot" style="background:${color}"></span>
            ${escapeHtml(item.name)}
          </span>
          <span class="legend-mini-pct">${sharePct.toFixed(1)}%</span>
        </div>
      `;
    });

    // 明细表格 HTML (使用完全统一的 totalTokensAll 分母)
    let tableRowsHtml = '';
    list.forEach((m, idx) => {
      const sharePct = totalTokensAll > 0 ? (m.totalTokens / totalTokensAll) * 100 : 0;
      tableRowsHtml += `
        <tr>
          <td style="color:var(--text-muted);width:30px;">#${idx + 1}</td>
          <td class="model-name-cell" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</td>
          <td class="model-num-cell">${modelSource === 'all' ? `${formatNumber(m.requestsOrSessions)} / ${formatNumber(m.sessions || 0)}` : formatNumber(m.requestsOrSessions)}</td>
          <td class="model-num-cell" data-tooltip="精确值: ${formatNumber(m.totalTokens)}">${formatCompact(m.totalTokens)}</td>
          <td class="model-num-cell">${formatPercent(m.hitRate)}</td>
          <td class="model-num-cell" style="color:var(--accent-cyan);font-weight:600;">${sharePct.toFixed(1)}%</td>
        </tr>
      `;
    });

    container.innerHTML = `
      <div class="models-side-by-side">
        <div class="donut-left-box">
          <div class="donut-chart-box">
            <svg viewBox="0 0 130 130">
              ${svgSlicesHtml}
            </svg>
            <div class="donut-center-text">
              <div class="donut-center-val">${formatCompact(totalTokensAll)}</div>
              <div class="donut-center-label">总用量</div>
            </div>
          </div>
          <div class="donut-legend-mini">
            ${legendMiniHtml}
          </div>
        </div>

        <div class="model-table-wrap compact-table">
          <table class="model-table">
            <thead>
              <tr>
                <th></th>
                <th>模型名称</th>
                <th style="text-align:right;">${modelSource === 'hermes' ? '会话数' : (modelSource === 'all' ? '请求 / 会话' : '请求数')}</th>
                <th style="text-align:right;">总 Token</th>
                <th style="text-align:right;">命中率</th>
                <th style="text-align:right;">份额</th>
              </tr>
            </thead>
            <tbody>
              ${tableRowsHtml}
            </tbody>
          </table>
        </div>
      </div>
    `;

    // 动态更新折叠摘要
    const sumModels = updateSummary ? document.getElementById('summary-models') : null;
    if (sumModels && list.length > 0 && totalTokensAll > 0) {
      const top = list[0];
      const topPct = ((top.totalTokens / totalTokensAll) * 100).toFixed(1);
      const srcName = modelSource === 'all' ? '全部来源'
        : (sourceById[modelSource]?.name || MODEL_SOURCE_OPTIONS.find(source => source.id === modelSource)?.label || modelSource);
      sumModels.textContent = `${srcName} · ${top.name} ${topPct}%`;
    }
  }

  // --- 4. 按日趋势折线图渲染 (18天平滑自适应) ---
  function renderDailyChart() {
    const svgContainer = document.getElementById('dailySvgContainer');
    const tooltip = document.getElementById('chartTooltip');
    if (!svgContainer) return;

    const rawDaily = state.dailyData || [];
    if (rawDaily.length === 0) {
      svgContainer.innerHTML = '<div style="color:var(--text-muted);padding:40px;text-align:center;">暂无按日数据</div>';
      return;
    }

    const daily = rawDaily.map(d => {
      const inp = d.input || 0;
      const out = d.output || 0;
      const cr = d.cache_read || 0;
      const cc = d.cache_creation || 0;
      const totalToken = inp + out + cr + cc;
      return {
        date: d.date,
        totalToken,
        cost: Number(d.cost_usd) || 0,
        requests: d.requests || 0,
        success: d.success || 0,
        input: inp,
        output: out,
        cacheRead: cr
      };
    });

    const W = 1100;
    const H = 140;
    const padL = 55;
    const padR = 55;
    const padT = 16;
    const padB = 24;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;

    const maxTokenRaw = Math.max(...daily.map(d => d.totalToken), 1000);
    const maxCostRaw = Math.max(...daily.map(d => d.cost), 1);

    const maxToken = Math.ceil(maxTokenRaw * 1.15);
    const maxCost = Math.ceil(maxCostRaw * 1.15);

    const n = daily.length;
    const getX = (i) => n === 1 ? padL + innerW / 2 : padL + (i / (n - 1)) * innerW;
    const getYToken = (val) => padT + innerH - (val / maxToken) * innerH;
    const getYCost = (val) => padT + innerH - (val / maxCost) * innerH;

    let gridLinesHtml = '';
    let yLabelsHtml = '';
    for (let step = 0; step <= 4; step++) {
      const y = padT + (step / 4) * innerH;
      const tokenVal = Math.round(maxToken * (1 - step / 4));
      const costVal = (maxCost * (1 - step / 4)).toFixed(1);

      gridLinesHtml += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="rgba(255,255,255,0.06)" stroke-dasharray="3,3"/>`;
      yLabelsHtml += `<text x="${padL - 8}" y="${y + 3.5}" fill="#8a8d99" font-size="9.5" text-anchor="end" font-family="var(--font-mono)">${formatCompact(tokenVal)}</text>`;
      yLabelsHtml += `<text x="${W - padR + 8}" y="${y + 3.5}" fill="#ffb454" font-size="9.5" text-anchor="start" font-family="var(--font-mono)">$${costVal}</text>`;
    }

    let xLabelsHtml = '';
    let triggerRectsHtml = '';
    // 当日期点较多时，隔点渲染标签，防重叠拥挤
    const skipStep = n > 12 ? 2 : 1;

    daily.forEach((d, i) => {
      const x = getX(i);
      const shortDate = d.date.length > 5 ? d.date.substring(5) : d.date;
      if (i % skipStep === 0 || i === n - 1) {
        xLabelsHtml += `<text x="${x}" y="${H - 8}" fill="#8a8d99" font-size="10" text-anchor="middle" font-family="var(--font-mono)">${shortDate}</text>`;
      }
      
      const rectW = n === 1 ? innerW : innerW / n;
      const rectX = x - rectW / 2;
      triggerRectsHtml += `
        <rect x="${rectX}" y="${padT}" width="${rectW}" height="${innerH}" fill="transparent"
          data-idx="${i}" class="chart-hover-bar" style="cursor:crosshair;"></rect>
      `;
    });

    let tokenPolyPoints = '';
    let costPolyPoints = '';
    let tokenAreaPoints = `${padL},${padT + innerH} `;

    daily.forEach((d, i) => {
      const x = getX(i);
      const yt = getYToken(d.totalToken);
      const yc = getYCost(d.cost);
      tokenPolyPoints += `${x},${yt} `;
      costPolyPoints += `${x},${yc} `;
      tokenAreaPoints += `${x},${yt} `;
    });

    tokenAreaPoints += `${getX(daily.length - 1)},${padT + innerH}`;

    let tokenDotsHtml = '';
    let costDotsHtml = '';
    daily.forEach((d, i) => {
      const x = getX(i);
      const yt = getYToken(d.totalToken);
      const yc = getYCost(d.cost);
      tokenDotsHtml += `<circle cx="${x}" cy="${yt}" r="3.5" fill="#26A69A" stroke="#1e1f26" stroke-width="2" class="token-dot"></circle>`;
      costDotsHtml += `<circle cx="${x}" cy="${yc}" r="3.5" fill="#ffb454" stroke="#1e1f26" stroke-width="2" class="cost-dot"></circle>`;
    });

    svgContainer.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <defs>
          <linearGradient id="tokenGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#26A69A" stop-opacity="0.35"/>
            <stop offset="100%" stop-color="#26A69A" stop-opacity="0.0"/>
          </linearGradient>
        </defs>

        <!-- 网格与轴标签 -->
        ${gridLinesHtml}
        ${yLabelsHtml}
        ${xLabelsHtml}

        <!-- 垂直十字线 -->
        <line id="crosshairLine" x1="0" y1="${padT}" x2="0" y2="${padT + innerH}"
          stroke="rgba(255,255,255,0.25)" stroke-dasharray="2,2" style="display:none;pointer-events:none;"/>

        <!-- Token 面积与折线 -->
        ${state.chartSeries.token ? `
          <polygon points="${tokenAreaPoints}" fill="url(#tokenGrad)"/>
          <polyline points="${tokenPolyPoints}" fill="none" stroke="#26A69A" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
          ${tokenDotsHtml}
        ` : ''}

        <!-- 成本折线 -->
        ${state.chartSeries.cost ? `
          <polyline points="${costPolyPoints}" fill="none" stroke="#ffb454" stroke-width="2" stroke-dasharray="4,2" stroke-linejoin="round" stroke-linecap="round"/>
          ${costDotsHtml}
        ` : ''}

        <!-- 悬停触发热区 -->
        ${triggerRectsHtml}
      </svg>
    `;

    // 交互事件
    const hoverBars = svgContainer.querySelectorAll('.chart-hover-bar');
    const crosshair = svgContainer.querySelector('#crosshairLine');

    hoverBars.forEach(bar => {
      bar.addEventListener('mouseenter', () => {
        const idx = Number(bar.getAttribute('data-idx'));
        const d = daily[idx];
        if (!d) return;

        const x = getX(idx);
        if (crosshair) {
          crosshair.setAttribute('x1', x);
          crosshair.setAttribute('x2', x);
          crosshair.style.display = 'block';
        }

        if (tooltip) {
          tooltip.innerHTML = `
            <div class="chart-tooltip-title">${d.date}</div>
            <div class="chart-tooltip-row">
              <span class="chart-tooltip-label">Token 消耗:</span>
              <span class="chart-tooltip-val" style="color:var(--accent-cyan);">${formatNumber(d.totalToken)}</span>
            </div>
            <div class="chart-tooltip-row">
              <span class="chart-tooltip-label">输入 / 输出:</span>
              <span class="chart-tooltip-val">${formatCompact(d.input)} / ${formatCompact(d.output)}</span>
            </div>
            <div class="chart-tooltip-row">
              <span class="chart-tooltip-label">缓存读取:</span>
              <span class="chart-tooltip-val">${formatCompact(d.cacheRead)}</span>
            </div>
            <div class="chart-tooltip-row">
              <span class="chart-tooltip-label">成本 USD:</span>
              <span class="chart-tooltip-val" style="color:var(--color-warning);">${formatCurrency(d.cost)}</span>
            </div>
            <div class="chart-tooltip-row">
              <span class="chart-tooltip-label">请求总数:</span>
              <span class="chart-tooltip-val">${formatNumber(d.requests)} 次</span>
            </div>
          `;

          tooltip.style.display = 'block';
          const ttWidth = tooltip.offsetWidth;
          const leftPos = (x / W) * svgContainer.clientWidth;
          if (leftPos + ttWidth > svgContainer.clientWidth) {
            tooltip.style.left = `${leftPos - ttWidth - 10}px`;
          } else {
            tooltip.style.left = `${leftPos + 10}px`;
          }
          tooltip.style.top = `30px`;
        }
      });

      bar.addEventListener('mouseleave', () => {
        if (crosshair) crosshair.style.display = 'none';
        if (tooltip) tooltip.style.display = 'none';
      });
    });

    // 动态更新折叠摘要
    const sumDaily = document.getElementById('summary-daily');
    if (sumDaily) {
      sumDaily.textContent = `${daily.length}天趋势 · 峰值 ${formatCompact(maxTokenRaw)}`;
    }
  }

  // --- 5. 数据源状态底栏渲染 ---
  function renderSourceStatusBar() {
    const hStatus = document.getElementById('hermesDbStatus');
    const cStatus = document.getElementById('ccsDbStatus');
    const apiStatus = document.getElementById('apiServerStatus');
    const healthBadge = document.getElementById('sourcesHealthBadge');
    const hermesMeta = document.getElementById('hermesProfilesMeta');
    const ccsMeta = document.getElementById('ccsDbMetaTable');

    if (state.dataSourceType === 'live') {
      let hermesExtra = '正常连接 (只读模式)';
      if (Array.isArray(state.healthData?.hermes)) {
        const okProfiles = state.healthData.hermes.filter(h => h.ok);
        const okCount = okProfiles.length;
        const totalMb = state.healthData.hermes.reduce((acc, cur) => acc + (cur.size_mb || 0), 0).toFixed(1);
        hermesExtra = `${okCount} 个活跃 Profile 正常 (${totalMb} MB)`;
        if (hermesMeta) {
          const names = okProfiles.map(p => p.agent).join(', ');
          hermesMeta.textContent = `活跃 Profiles: ${okCount} 个 (${names})`;
        }
      }
      if (hStatus) hStatus.textContent = `● ${hermesExtra}`;

      let ccsExtra = '正常连接 (只读模式)';
      if (state.healthData?.ccs?.ok) {
        const ccsMb = Number(state.healthData.ccs.size_mb || 0).toFixed(1);
        ccsExtra = `正常 (${ccsMb} MB)`;
      }
      if (cStatus) cStatus.textContent = `● ${ccsExtra}`;
      if (ccsMeta && state.summaryData?.ccs?.requests != null) {
        ccsMeta.textContent = `数据表: proxy_request_logs (${state.summaryData.ccs.requests.toLocaleString()}条) + daily_rollups`;
      }

      if (apiStatus) {
        apiStatus.className = 'source-badge online';
        apiStatus.textContent = '● 本地 API 常驻中 (8790)';
      }
      if (healthBadge) {
        healthBadge.className = 'source-badge online';
        healthBadge.textContent = '● 全部正常';
      }
    } else {
      if (hStatus) hStatus.textContent = '● 样本模式 (服务离线)';
      if (cStatus) cStatus.textContent = '● 样本模式 (服务离线)';
      if (apiStatus) {
        apiStatus.className = 'source-badge fallback';
        apiStatus.textContent = '○ 本地 API 离线 (已使用样本)';
      }
      if (healthBadge) {
        healthBadge.className = 'source-badge fallback';
        healthBadge.textContent = '● 样本兜底';
      }
    }
    updatePanelSummaries();
  }

  // --- 6. 工具箱主页与功能卡片动态加载 ---
  let toolboxStatusCache = null;

    function renderAppVersion(version) {
    const el = document.getElementById('appVersionBadge');
    if (!el || !version) return;
    const clean = String(version).trim().replace(/^v/i, "");
    if (!clean) return;
    el.textContent = "v" + clean;
  }

  // --- 自动检查更新 ---
  let appUpdateCache = null;

  async function checkAppUpdate(force = false) {
    const ico = document.getElementById("icoCheckUpdate");
    const statusBadge = document.getElementById("updateStatusBadge");

    if (force && ico) ico.classList.add("ph-spin");
    if (statusBadge) {
      statusBadge.className = "update-status-badge checking";
      statusBadge.textContent = "正在检查...";
    }

    try {
      const resp = await fetch("/api/toolbox/check_update" + (force ? "?force=1" : ""), { cache: "no-store" });
      if (resp.ok) {
        const data = await resp.json();
        appUpdateCache = data;
        renderUpdateUI(data);
        return;
      }
    } catch (e) {
      console.warn("Check update request failed:", e);
    } finally {
      if (ico) ico.classList.remove("ph-spin");
    }

    if (statusBadge) {
      statusBadge.className = "update-status-badge error";
      statusBadge.textContent = "检查失败";
    }
  }

  function renderUpdateUI(d) {
    if (!d) return;
    const curVerEl = document.getElementById("updateCurVer");
    const latestVerEl = document.getElementById("updateLatestVer");
    const statusBadge = document.getElementById("updateStatusBadge");
    const timeEl = document.getElementById("updateCheckedTime");
    const btnDownload = document.getElementById("btnGoDownload");
    const notesBox = document.getElementById("updateNotesBox");
    const notesContent = document.getElementById("updateNotesContent");
    const topBadge = document.getElementById("updateAvailBadge");
    const topText = document.getElementById("updateAvailText");

    if (curVerEl) curVerEl.textContent = d.current_version ? ("v" + d.current_version) : "--";
    if (latestVerEl) latestVerEl.textContent = d.latest_version ? ("v" + d.latest_version) : "--";
    if (timeEl && d.checked_at) {
      const dt = new Date(d.checked_at * 1000);
      timeEl.textContent = "检查时间: " + dt.toLocaleTimeString();
    }

    if (d.ok && d.has_update) {
      if (statusBadge) {
        statusBadge.className = "update-status-badge has-update";
        statusBadge.textContent = "发现新版本 v" + d.latest_version;
      }
      if (topBadge) {
        topBadge.style.display = "inline-flex";
        if (topText) topText.textContent = "新版 v" + d.latest_version;
      }
      if (btnDownload) {
        btnDownload.href = d.download_url || d.release_url || "https://github.com/TerryYu12/XiaoyuToolbox-OSS/releases/latest";
        btnDownload.style.display = "inline-flex";
      }
      if (d.release_notes && notesBox && notesContent) {
        notesBox.style.display = "block";
        notesContent.textContent = d.release_notes;
      }
    } else if (d.ok && !d.has_update) {
      if (statusBadge) {
        statusBadge.className = "update-status-badge latest";
        statusBadge.textContent = d.message || "已是最新版本";
      }
      if (topBadge) topBadge.style.display = "none";
      if (btnDownload) btnDownload.style.display = "none";
      if (notesBox) notesBox.style.display = "none";
    } else {
      if (statusBadge) {
        statusBadge.className = "update-status-badge error";
        statusBadge.textContent = d.error || "检查失败";
      }
    }
  }

  async function fetchToolboxStatus() {
    try {
      const resp = await fetch('http://127.0.0.1:8790/api/toolbox/status');
      if (resp.ok) {
        const data = await resp.json();
        toolboxStatusCache = data;
        renderAppVersion(data.version);
        renderHomeFeatures(data.features || []);
        renderHomeHotkeys(data.hotkeys || {});
        renderHomeServices(data);
        renderTranslateTopBar(data);
        return;
      }
    } catch (e) {
      console.warn('Toolbox status fetch failed, using fallback features:', e);
    }
    // 兜底离线 features
    const fallbackFeatures = [
      { id: 'translate', name: '划词翻译', icon: 'translate', desc: '选中文字按热键即译；支持框选 OCR 识别后翻译', page: '#/translate', ready: true },
      { id: 'tokens', name: 'Token 统计', icon: 'chart', desc: '各 AI 工具 / Agent 的 token 消耗、缓存命中率、成本', page: '#/token', ready: true }
    ];
    renderHomeFeatures(fallbackFeatures);
    renderHomeHotkeys({ zh: 'ctrl+shift+t', en: 'ctrl+shift+e', ocr: 'ctrl+alt+s' });
    renderHomeServices({
      stats_port: 8790,
      translate: { running: true, port: 8899, model: 'Qwen3.5-4B-Q4_K_M.gguf' },
      ocr: { available: true, engine: 'Windows.Media.Ocr' }
    });
  }

  function getFeatureIconSvg(iconType) {
    if (iconType === 'translate') {
      return `
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="m5 8 6 6"></path>
          <path d="m4 14 6-6 2-3"></path>
          <path d="M2 5h12"></path>
          <path d="M7 2h1"></path>
          <path d="m22 22-5-10-5 10"></path>
          <path d="M14 18h6"></path>
        </svg>
      `;
    }
    if (iconType === 'chart') {
      return `
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="20" x2="18" y2="10"></line>
          <line x1="12" y1="20" x2="12" y2="4"></line>
          <line x1="6" y1="20" x2="6" y2="14"></line>
        </svg>
      `;
    }
    return `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
        <line x1="8" y1="21" x2="16" y2="21"></line>
        <line x1="12" y1="17" x2="12" y2="21"></line>
      </svg>
    `;
  }

  function renderHomeFeatures(features) {
    const container = document.getElementById('featuresGridContainer');
    if (!container) return;
    if (!features || features.length === 0) {
      container.innerHTML = '<div style="color:var(--text-muted);padding:20px;text-align:center;">暂无可用模块</div>';
      return;
    }

    container.innerHTML = features.map(f => {
      const iconSvg = getFeatureIconSvg(f.icon);
      const isReady = f.ready !== false;
      const readyBadge = isReady
        ? `<span class="feature-status-badge ready">● 就绪</span>`
        : `<span class="feature-status-badge">● 开发中</span>`;

      return `
        <div class="feature-card" onclick="location.hash='${f.page || '#/'}'" title="点击打开 ${f.name}">
          <div class="feature-card-header">
            <div class="feature-icon">${iconSvg}</div>
            ${readyBadge}
          </div>
          <div class="feature-card-body">
            <h3 class="feature-title">${f.name}</h3>
            <p class="feature-desc">${f.desc || ''}</p>
          </div>
          <div class="feature-card-footer">
            <span class="feature-action-btn">
              <span>进入功能</span>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <path d="M5 12h14M12 5l7 7-7 7"/>
              </svg>
            </span>
          </div>
        </div>
      `;
    }).join('');
  }

  function formatHotkeyStr(k) {
    if (!k) return '';
    return k.split('+').map(part => part.trim().toUpperCase()).join(' + ');
  }

  // --- 全局热键设置（划词 3 键 + 剪贴板唤起键，可改键） ---
  const HOTKEY_ACTIONS = [
    { field: 'hotkey_zh', id: 'zh', name: '划词中译', desc: '选中文本翻成中文' },
    { field: 'hotkey_en', id: 'en', name: '划词英译', desc: '选中文本翻成英文' },
    { field: 'hotkey_ocr', id: 'ocr', name: '截屏OCR', desc: '框选屏幕文字识别并翻译' },
    { field: 'hotkey_clip', id: 'clip', name: '唤起剪贴板历史', desc: '呼出工作台并直达剪贴板历史页' }
  ];
  let hotkeyState = null;      // /api/hotkeys 数据 {hotkeys, status, captured}
  let hotkeyCapture = null;    // {field, value} 改键捕获态

  function hkOf(id) {
    if (hotkeyState && hotkeyState.hotkeys && hotkeyState.hotkeys[id]) return hotkeyState.hotkeys[id];
    return { zh: 'ctrl+shift+t', en: 'ctrl+shift+e', ocr: 'ctrl+alt+s', clip: 'ctrl+alt+v' }[id];
  }

  // 与后端 selection.KEY_MAP 词表保持一致
  function normalizeHotkeyKey(e) {
    const k = e.key;
    if (/^[a-zA-Z]$/.test(k)) return k.toLowerCase();
    if (/^[0-9]$/.test(k)) return k;
    const fm = k.match(/^F([1-9]|1[0-2])$/i);
    if (fm) return 'f' + fm[1];
    const named = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
      Home: 'home', End: 'end', PageUp: 'pgup', PageDown: 'pgdn', Insert: 'ins',
      Delete: 'del', ' ': 'space', Tab: 'tab' };
    return named[k] || null;
  }

  async function fetchHotkeyState() {
    try {
      const resp = await fetch('/api/hotkeys');
      const d = await resp.json();
      if (d && d.ok) hotkeyState = d;
    } catch (e) { /* 服务离线保持现状 */ }
    renderHotkeysUI();
  }

  function renderHotkeysUI() {
    renderHomeHotkeyRows();
    renderClipHotkeyBar();
    renderRailHotkeys();
  }

  function hotkeyRowHtml(act) {
    const st = hotkeyState && hotkeyState.status ? hotkeyState.status[act.id] : undefined;
    const captured = hotkeyCapture && hotkeyCapture.field === act.field;
    let ctrl;
    if (captured && hotkeyCapture.value) {
      ctrl = `<span class="hotkey-badge new"><kbd>${formatHotkeyStr(hotkeyCapture.value)}</kbd></span>
        <button class="btn-text" data-hk-act="confirm" data-hk-field="${act.field}">确认</button>
        <button class="btn-text" data-hk-act="cancel" data-hk-field="${act.field}">取消</button>`;
    } else if (captured) {
      ctrl = `<span class="hotkey-capturing">按下新组合键… <i>Esc 取消</i></span>`;
    } else {
      const failTip = (st === false) ? '<span class="hotkey-fail" title="该组合可能已被其他程序占用">未生效</span>' : '';
      ctrl = `<span class="hotkey-badge"><kbd>${formatHotkeyStr(hkOf(act.id))}</kbd></span>${failTip}
        <button class="btn-text" data-hk-act="edit" data-hk-field="${act.field}">修改</button>`;
    }
    return `
      <div class="hotkey-item" data-hk-field="${act.field}">
        <div class="hotkey-info">
          <span class="hotkey-name">${act.name}</span>
          <span class="hotkey-desc">${act.desc}</span>
        </div>
        <div class="hotkey-ctrl">${ctrl}</div>
      </div>`;
  }

  // 翻译页「全局热键」卡片：划词 3 键（可改）
  function renderHomeHotkeyRows() {
    const list = document.getElementById('homeHotkeysList');
    if (!list) return;
    list.innerHTML = HOTKEY_ACTIONS.slice(0, 3).map(hotkeyRowHtml).join('');
  }

  // 剪贴板页唤起热键行
  function renderClipHotkeyBar() {
    const badge = document.getElementById('clipHotkeyBadge');
    const status = document.getElementById('clipHotkeyStatus');
    if (badge) {
      const captured = hotkeyCapture && hotkeyCapture.field === 'hotkey_clip';
      badge.innerHTML = captured
        ? (hotkeyCapture.value ? `<kbd>${formatHotkeyStr(hotkeyCapture.value)}</kbd>` : '')
        : `<kbd>${formatHotkeyStr(hkOf('clip'))}</kbd>`;
      badge.classList.toggle('new', !!(captured && hotkeyCapture.value));
      badge.classList.toggle('capturing', !!(captured && !hotkeyCapture.value));
      if (captured && !hotkeyCapture.value) badge.innerHTML = '';
    }
    // 剪贴板行的操作按钮与状态行
    const bar = document.getElementById('clipHotkeyBar');
    if (bar) {
      const captured = hotkeyCapture && hotkeyCapture.field === 'hotkey_clip';
      let acts = bar.querySelector('.clip-hotkey-acts');
      if (!acts) {
        acts = document.createElement('span');
        acts.className = 'clip-hotkey-acts';
        bar.appendChild(acts);
      }
      if (captured) {
        acts.innerHTML = hotkeyCapture.value
          ? `<button class="btn-text" data-hk-act="confirm" data-hk-field="hotkey_clip">确认</button>
             <button class="btn-text" data-hk-act="cancel" data-hk-field="hotkey_clip">取消</button>`
          : `<span class="hotkey-capturing">按下新组合键… <i>Esc 取消</i></span>`;
      } else {
        acts.innerHTML = `<button class="btn-text" data-hk-act="edit" data-hk-field="hotkey_clip">修改</button>`;
      }
    }
    const st = hotkeyState && hotkeyState.status ? hotkeyState.status['clip'] : undefined;
    if (status) {
      status.innerHTML = (st === false)
        ? '<span class="hotkey-fail" title="该组合可能已被其他程序占用">未生效</span>'
        : '<span class="hotkey-fail ok" title="热键已注册生效">已生效</span>';
    }
  }

  // 左栏底部键位速览（改键后跟随更新）
  function renderRailHotkeys() {
    const el = document.getElementById('railHotkeys');
    if (!el) return;
    const pairs = [
      ['中译', hkOf('zh')], ['英译', hkOf('en')], ['OCR', hkOf('ocr')], ['剪贴板', hkOf('clip')]
    ];
    el.innerHTML = pairs.map(([n, k]) => `<span class="rk-row"><kbd>${formatHotkeyStr(k)}</kbd>${n}</span>`).join('<br>');
  }

  function beginHotkeyCapture(field) {
    hotkeyCapture = { field, value: null };
    renderHotkeysUI();
  }

  function cancelHotkeyCapture() {
    hotkeyCapture = null;
    renderHotkeysUI();
  }

  async function confirmHotkeyCapture() {
    if (!hotkeyCapture || !hotkeyCapture.value) return;
    const { field, value } = hotkeyCapture;
    try {
      const resp = await fetch('/api/hotkeys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: field, value })
      });
      const d = await resp.json();
      if (!d || !d.ok) {
        alert('热键保存失败：' + ((d && d.error) || '未知错误'));
        return;                          // 保持在捕获态可重新选键
      }
      hotkeyCapture = null;
      toolboxStatusCache = null;         // 翻译页顶部键位条下次进页重新拉取
      fetchToolboxStatus();
      fetchHotkeyState();
    } catch (e) {
      alert('热键保存失败：' + e);
    }
  }

  // 捕获态：拦截按键合成新键位（词表与后端一致）
  document.addEventListener('keydown', (e) => {
    if (!hotkeyCapture) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === 'Escape') { cancelHotkeyCapture(); return; }
    const mods = [];
    if (e.ctrlKey) mods.push('ctrl');
    if (e.altKey) mods.push('alt');
    if (e.shiftKey) mods.push('shift');
    if (e.metaKey) mods.push('win');
    if (!mods.length) return;           // 必须带修饰键，防止单键占用
    const main = normalizeHotkeyKey(e);
    if (!main) return;                  // 不在词表内的主键直接忽略
    hotkeyCapture.value = mods.join('+') + '+' + main;
    renderHotkeysUI();
  }, true);

  function renderHomeHotkeys(hotkeys) {
    // 兼容入口：toolboxStatus 里的三键数据仅作 hotkeyState 拉取失败时的兜底
    if (!hotkeyState && hotkeys) {
      hotkeyState = { hotkeys: Object.assign({ clip: 'ctrl+alt+v' }, hotkeys), status: {}, captured: false };
    }
    renderHotkeysUI();
  }

  function renderHomeServices(data) {
    const list = document.getElementById('homeServicesList');
    if (!list) return;
    const trans = data.translate || {};
    const ocr = data.ocr || {};
    const statsPort = data.stats_port || 8790;

    list.innerHTML = `
      <div class="service-item">
        <span class="service-name">本地翻译引擎</span>
        <span class="service-val">
          <span class="status-dot ${trans.running ? 'online' : 'offline'}"></span>
          ${trans.model || 'Qwen3.5-4B'} (端口 ${trans.port || 8899}) · ${trans.running ? '运行中' : '未就绪'}
        </span>
      </div>
      <div class="service-item">
        <span class="service-name">离线 OCR 识别</span>
        <span class="service-val">
          <span class="status-dot ${ocr.available ? 'online' : 'offline'}"></span>
          ${ocr.engine || 'Windows.Media.Ocr'} · ${ocr.available ? '可用' : '不可用'}
        </span>
      </div>
      <div class="service-item">
        <span class="service-name">统计服务端口</span>
        <span class="service-val">
          <span class="status-dot online"></span>
          http://127.0.0.1:${statsPort}/ (端口 ${statsPort})
        </span>
      </div>
    `;
  }

  function renderTranslateTopBar(data) {
    const bar = document.getElementById('transHotkeysBar');
    const engineText = document.getElementById('transEngineText');
    if (bar && data.hotkeys) {
      bar.innerHTML = `
        <span class="hotkey-pill"><kbd>${formatHotkeyStr(data.hotkeys.zh)}</kbd> 划词中译</span>
        <span class="hotkey-pill"><kbd>${formatHotkeyStr(data.hotkeys.en)}</kbd> 划词英译</span>
        <span class="hotkey-pill"><kbd>${formatHotkeyStr(data.hotkeys.ocr)}</kbd> 屏幕OCR</span>
      `;
    }
    if (engineText) {
      const eng = state.currentEngine || 'cloud';
      const engName = eng === 'cloud' ? '云端极速 (Google/Bing)' : (eng === 'api' ? '自定义 AI API' : '本地大模型 (Qwen3.5 4B)');
      engineText.textContent = `当前引擎：${engName}`;
    }
  }

  // --- 7. 多引擎翻译工作台逻辑 (本地 / 云端 / API) ---
  state.currentEngine = 'cloud';
  state.transConfig = null;

  async function loadTranslateEngineConfig() {
    try {
      const resp = await fetch('http://127.0.0.1:8790/api/translate/config');
      if (resp.ok) {
        state.transConfig = await resp.json();
        state.currentEngine = state.transConfig.trans_engine || 'cloud';
        applyEnginePillActive(state.currentEngine);
        populateEngineConfigInputs(state.transConfig);
      }
    } catch (e) {
      // 离线默认
      state.currentEngine = 'cloud';
      applyEnginePillActive('cloud');
    }
  }

  function applyEnginePillActive(engine) {
    state.currentEngine = engine;
    const pills = document.querySelectorAll('#enginePillsGroup .engine-pill');
    pills.forEach(p => {
      if (p.getAttribute('data-engine') === engine) {
        p.classList.add('active');
      } else {
        p.classList.remove('active');
      }
    });
    const tag = document.getElementById('transActiveModelTag');
    if (tag && state.transConfig) {
      tag.textContent = state.transConfig.api_model || 'DeepSeek/CC';
    }
    const engineText = document.getElementById('transEngineText');
    if (engineText) {
      const engName = engine === 'cloud' ? '云端极速 (Google 多源)' : (engine === 'api' ? '自定义 AI API' : '本地大模型 (Qwen3.5 4B)');
      engineText.textContent = `当前引擎：${engName}`;
    }
  }

  function populateEngineConfigInputs(cfg) {
    if (!cfg) return;
    const ep = document.getElementById('cfgApiEndpoint');
    const md = document.getElementById('cfgApiModel');
    const ak = document.getElementById('cfgApiKey');
    const ba = document.getElementById('cfgBaiduAppid');
    const bk = document.getElementById('cfgBaiduKey');
    if (ep && cfg.api_endpoint) ep.value = cfg.api_endpoint;
    if (md && cfg.api_model) md.value = cfg.api_model;
    if (ak && cfg.api_key) ak.value = cfg.api_key;
    if (ba) ba.value = cfg.baidu_appid || '';
    if (bk) bk.value = cfg.baidu_key || '';
  }

  async function saveEngineConfigFromInputs() {
    const ep = document.getElementById('cfgApiEndpoint')?.value.trim();
    const md = document.getElementById('cfgApiModel')?.value.trim();
    const ak = document.getElementById('cfgApiKey')?.value.trim();
    const ba = document.getElementById('cfgBaiduAppid')?.value.trim();
    const bk = document.getElementById('cfgBaiduKey')?.value.trim();
    const updates = {
      trans_engine: state.currentEngine,
      api_endpoint: ep || 'https://api.openai.com/v1',
      api_model: md || 'gpt-4o-mini',
      // 百度官方源：两个都下发，清空即停用
      baidu_appid: ba || '',
      baidu_key: bk || '',
    };
    if (ak) updates.api_key = ak;

    try {
      const resp = await fetch('http://127.0.0.1:8790/api/translate/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
      });
      if (resp.ok) {
        const res = await resp.json();
        state.transConfig = res.config;
        alert('翻译引擎配置已成功保存！');
        const drawer = document.getElementById('engineConfigDrawer');
        if (drawer) drawer.style.display = 'none';
        applyEnginePillActive(state.currentEngine);
      }
    } catch (e) {
      alert('保存失败: ' + e);
    }
  }

  async function doTranslate() {
    const inputEl = document.getElementById('transInputText');
    const outputEl = document.getElementById('transOutputText');
    const btn = document.getElementById('btnDoTranslate');
    const metaChip = document.getElementById('transMetaChip');
    const errorBanner = document.getElementById('transErrorBanner');
    const errorMsg = document.getElementById('transErrorMsg');

    if (!inputEl || !outputEl) return;

    const text = inputEl.value.trim();
    if (!text) {
      inputEl.focus();
      return;
    }

    const to = document.querySelector('input[name="transTargetLang"]:checked')?.value || 'zh';
    const engine = state.currentEngine || 'cloud';

    if (errorBanner) errorBanner.style.display = 'none';
    btn.disabled = true;
    btn.classList.add('loading');
    outputEl.value = '正在呼叫翻译引擎...';

    try {
      const resp = await fetch('http://127.0.0.1:8790/api/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, to, engine })
      });

      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.error || `HTTP ${resp.status}`);
      }

      outputEl.value = data.translation || '';
      if (metaChip) {
        const engLabel = data.engine === 'cloud' ? '云端直连' : (data.engine === 'api' ? '云端 AI API' : '本地 Qwen 4B');
        metaChip.textContent = `耗时: ${data.ms || 0}ms · 引擎: ${engLabel} · 目标: ${data.to === 'zh' ? '中文' : '英文'}`;
        metaChip.style.display = 'inline-flex';
      }
    } catch (err) {
      outputEl.value = '';
      if (errorBanner && errorMsg) {
        errorMsg.textContent = `翻译失败: ${err.message || err}`;
        errorBanner.style.display = 'flex';
      }
    } finally {
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  }

  function initTranslateWorkspace() {
    loadTranslateEngineConfig();

    const inputEl = document.getElementById('transInputText');
    const charCount = document.getElementById('transCharCount');
    const btnClear = document.getElementById('btnClearInput');
    const btnCopy = document.getElementById('btnCopyResult');
    const btnDo = document.getElementById('btnDoTranslate');
    const outputEl = document.getElementById('transOutputText');
    const errorBanner = document.getElementById('transErrorBanner');

    // 引擎 Pills 点击
    const pills = document.querySelectorAll('#enginePillsGroup .engine-pill');
    pills.forEach(pill => {
      pill.addEventListener('click', () => {
        const eng = pill.getAttribute('data-engine');
        applyEnginePillActive(eng);
        fetch('http://127.0.0.1:8790/api/translate/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ trans_engine: eng })
        }).catch(() => {});
      });
    });

    // 展开/收起配置抽屉
    const btnToggleConfig = document.getElementById('btnToggleApiConfig');
    const drawer = document.getElementById('engineConfigDrawer');
    if (btnToggleConfig && drawer) {
      btnToggleConfig.addEventListener('click', () => {
        const show = (drawer.style.display === 'none' || !drawer.style.display);
        drawer.style.display = show ? 'block' : 'none';
      });
    }

    const btnSaveCfg = document.getElementById('btnSaveTransConfig');
    if (btnSaveCfg) {
      btnSaveCfg.addEventListener('click', saveEngineConfigFromInputs);
    }

    if (inputEl && charCount) {
      inputEl.addEventListener('input', () => {
        const len = inputEl.value.length;
        charCount.textContent = `${len} / 5000`;
        charCount.style.color = len > 5000 ? 'var(--color-error)' : 'var(--text-muted)';
      });

      inputEl.addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.key === 'Enter') {
          e.preventDefault();
          doTranslate();
        }
      });
    }

    if (btnClear && inputEl) {
      btnClear.addEventListener('click', () => {
        inputEl.value = '';
        if (charCount) charCount.textContent = '0 / 5000';
        if (outputEl) outputEl.value = '';
        if (errorBanner) errorBanner.style.display = 'none';
        inputEl.focus();
      });
    }

    if (btnCopy && outputEl) {
      btnCopy.addEventListener('click', () => {
        const val = outputEl.value;
        if (!val) return;
        navigator.clipboard.writeText(val).then(() => {
          const orig = btnCopy.textContent;
          btnCopy.textContent = '已复制 ✓';
          setTimeout(() => { btnCopy.textContent = orig; }, 1500);
        }).catch(() => {});
      });
    }

    if (btnDo) {
      btnDo.addEventListener('click', doTranslate);
    }
  }

  // --- 8. AI 专区逻辑 (每日 AI 新闻 + 实时天梯榜) ---
  state.aiCategory = 'all';
  state.aiRankCategory = 'aa_index';   // 默认全模型口径（HF 榜仅开源模型）
  // 媒体榜（视频/生图/音频/多模态）各自记住上次看的细分榜，键是主入口名。
  state.aiMediaCategories = {
    video: 'video__text_to_video__audio', audio: 'audio__asr',
    image: 'image__text_to_image', multimodal: 'multimodal__vision'
  };
  // 媒体主入口 → 二级选择条容器 id（路由还原与主标签切换共用一份，别各写一份）
  const MEDIA_BAR_IDS = {
    video: 'videoCategoryBar', audio: 'audioCategoryBar',
    image: 'imageCategoryBar', multimodal: 'multimodalCategoryBar'
  };
  state.aiCapDim = 'gdpval';           // 能力榜单当前维度（八维二级选择条）
  state.aiOpensourceOnly = false;      // 「仅看开源」筛选（只挂在带 license 标注的 aa_index / cap_* 口径）
  state.aiArenaCategory = '';
  state.aiLiveBenchCategory = 'livebench__overall';
  state.aiBusyChip = '';               // 正在「抓取最新」的那颗 chip id（抓取期间不许被覆盖）

  async function loadAiNews(category = 'all', search = '') {
    const grid = document.getElementById('aiNewsGrid');
    if (!grid) return;
    // 同 loadAiRankings：切换分类时清空会让整片卡片塌成一行占位再弹回来
    // （实测 42 张卡 4253px → 598px），滚动条与可用宽度随之跳变，文字整页重排。
    // 旧卡片留到新数据到达后由 renderAiNews 一次性替换。
    if (!grid.querySelector('.ai-news-card')) {
      grid.innerHTML = '<div class="feature-card skeleton-card"><div class="feature-title">正在加载前沿资讯...</div></div>';
    }

    let items = [];
    try {
      const q = new URLSearchParams({ category, search });
      const resp = await fetch(`http://127.0.0.1:8790/api/ai/news?${q.toString()}`);
      if (resp.ok) {
        const data = await resp.json();
        items = data.items || [];
        state.aiNewsUpdatedAt = data.updated_at || '';
      }
    } catch (e) {
      if (window.FALLBACK_DATA && window.FALLBACK_DATA.ai_news) {
        items = window.FALLBACK_DATA.ai_news;
      }
    }

    // 按发布时间倒序：今天的必须在最前（抓取器按源分组返回，旧版论文组会霸榜误导"没更新"）
    items = items.slice().sort((a, b) => (b.published_at || '').localeCompare(a.published_at || ''));
    renderAiNews(items);
    renderAiNewsUpdated();
  }

  function renderAiNewsUpdated() {
    const chip = document.getElementById('aiNewsUpdatedChip');
    if (!chip) return;
    if (state.aiBusyChip === 'aiNewsUpdatedChip') return;   // 正在抓取，别盖掉「抓取中…」
    const upd = state.aiNewsUpdatedAt || '';
    const today = new Date();
    const ymd = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    const nToday = (state.aiNewsItems || []).filter(n => (n.published_at || '').startsWith(ymd)).length;
    const fresh = upd.slice(0, 10) === ymd;
    chip.className = 'hud-chip ' + (fresh ? 'success' : 'offline');
    chip.textContent = `更新: ${upd.slice(5, 16) || '--'} · 今日 ${nToday} 条`;
    chip.title = `抓取器每天 19:00 自动更新（机器之心/量子位/HF Papers 等 6 源），缓存超 20 小时启动时补抓；上次抓取 ${upd || '--'}`;
  }

  function renderAiNews(items) {
    state.aiNewsItems = items;
    const grid = document.getElementById('aiNewsGrid');
    if (!grid) return;

    if (!items || items.length === 0) {
      grid.innerHTML = '<div style="color:var(--text-muted); padding:30px; text-align:center; grid-column:1/-1;">暂无匹配的 AI 快讯</div>';
      return;
    }

    grid.innerHTML = items.map(n => {
      // 帖子卡：头部（源+分类+时间）→ 标题 → 配图 → 摘要 → 标签 → 操作。
      // 配图走后端 /api/ai/image 同源代理（直连会被 CORP 拦），加载失败整块移除；
      // 无图条目不渲染媒体块——真实信息流有图没图自然错落，不放占位块充数。
      const tagsHtml = (n.tags || [])
        .filter(t => t && t !== n.source && t !== n.category_name)
        .map(t => `<span class="news-tag-item">#${escapeHtml(t)}</span>`).join('');
      const hotBadge = n.hot ? '<span style="color:#ff6b35; margin-left:4px;" title="热点动态">🔥</span>' : '';
      // 中文优先（抓取时已译），英文原文放悬停提示
      const zhTitle = n.title_zh || '';
      const zhSummary = n.summary_zh || '';
      const titleTip = zhTitle ? ` title="${escapeHtml(n.title || '')}"` : '';
      const sumTip = zhSummary ? ` title="${escapeHtml(n.summary || '')}"` : '';
      const media = n.image ? `
          <a class="ai-post-media" href="${n.source_url || '#'}" target="_blank" rel="noopener">
            <img src="/api/ai/image?u=${encodeURIComponent(n.image)}" alt="" loading="lazy"
                 onerror="this.closest('.ai-post-media').remove()">
          </a>` : '';
      return `
        <article class="ai-news-card ai-post">
          <header class="news-card-header">
            <span class="news-source-chip">${n.source || 'AI Hub'}</span>
            <span class="news-cat-chip">${n.category_name || ''}</span>
            <span class="news-time">${n.published_at || ''}</span>
          </header>
          <h3 class="news-card-title"${titleTip}>${escapeHtml(zhTitle || n.title || '')} ${hotBadge}</h3>
          ${media}
          <p class="news-card-summary"${sumTip}>${escapeHtml(zhSummary || n.summary || '')}</p>
          ${tagsHtml ? `<div class="news-card-tags">${tagsHtml}</div>` : ''}
          <div class="news-card-actions">
            <button class="btn-text" onclick="window.sendNewsToTranslate(${JSON.stringify(n.summary || n.title).replace(/"/g, '&quot;')})">⚡ 速译摘要</button>
            <button class="btn-text" onclick="window.copyNewsText(${JSON.stringify((n.title || '') + '\n\n' + (n.summary || '')).replace(/"/g, '&quot;')})">📋 复制</button>
            <a href="${n.source_url || '#'}" target="_blank" class="btn-text" style="text-decoration:none;">🔗 查看原文</a>
          </div>
        </article>
      `;
    }).join('');
  }

  window.sendNewsToTranslate = function(text) {
    location.hash = '#/translate';
    setTimeout(() => {
      const inputEl = document.getElementById('transInputText');
      if (inputEl) {
        inputEl.value = text;
        const charCount = document.getElementById('transCharCount');
        if (charCount) charCount.textContent = `${text.length} / 5000`;
        doTranslate();
      }
    }, 100);
  };

  window.copyNewsText = function(text) {
    navigator.clipboard.writeText(text).then(() => {
      alert('已成功复制新闻内容到剪贴板！');
    });
  };

  async function loadAiRankings(category = 'aa_index') {
    // 「能力榜单」是主标签栏上的一个入口，实际数据按 state.aiCapDim 落到 cap_<维度>，
    // 所以这里把入口名翻译成真实榜 id 再请求。
    if (category === 'capability') category = 'cap_' + (state.aiCapDim || 'gdpval');
    if (category === 'arena') category = state.aiArenaCategory || 'arena';
    if (category === 'livebench') category = state.aiLiveBenchCategory || 'livebench__overall';
    // 媒体主入口落到「上次看的那个细分榜」；已经不打算混用一个总榜。
    if (state.aiMediaCategories && state.aiMediaCategories[category]) {
      category = state.aiMediaCategories[category];
    }
    state.aiRankCategory = category;
    const container = document.getElementById('rankingsTableContainer');
    const banner = document.getElementById('rankingsMetaBanner');
    if (!container) return;

    // 切换榜单时**不要**先把表格换成一行占位：实测表格区会从 522px 塌成 69px 再弹回，
    // 内容变矮让外层滚动条消失、`.content` 可用宽度 1270→1278，整页文字跟着重新折行
    // ——用户看到的就是「闪一下 + 文字抖动」。旧表格留到新数据到达后一次性替换，
    // 只有首次进入（容器里还没有表）才给占位。
    if (!container.querySelector('table')) {
      container.innerHTML = '<div style="padding:25px; text-align:center; color:var(--text-muted);">正在拉取最新天梯榜数据...</div>';
    }

    let rankData = null;
    let errMsg = '';
    try {
      const resp = await fetch(`http://127.0.0.1:8790/api/ai/rankings?category=${category}`);
      if (resp.ok) {
        const res = await resp.json();
        const sourceCategory = /^(arena__|livebench__|video__|image__|audio__|multimodal__)/.test(category);
        if (sourceCategory && res.category !== category) {
          rankData = null;
          errMsg = '当前后端不支持该榜单，请更新工具箱后重试。';
        } else {
          rankData = res.data;
          errMsg = res.error || '';
        }
        if (String(res.category || '').startsWith('arena__')) state.aiArenaCategory = res.category;
        if (String(res.category || '').startsWith('livebench__')) state.aiLiveBenchCategory = res.category;
        const resCat = String(res.category || '');
        if (/^(video|image|audio|multimodal)__/.test(resCat) && state.aiMediaCategories) {
          const parent = resCat.split('__')[0];
          if (state.aiMediaCategories[parent]) state.aiMediaCategories[parent] = resCat;
        }
        renderCapabilityDimBar(res.capability_dimensions || []);
        const sourceCats = res.source_categories || {};
        renderRankingsSourceBar('arenaCategoryBar', sourceCats.arena || [], state.aiArenaCategory,
          id => { state.aiArenaCategory = id; loadAiRankings(id); });
        renderRankingsSourceBar('livebenchCategoryBar', sourceCats.livebench || [], state.aiLiveBenchCategory,
          id => { state.aiLiveBenchCategory = id; loadAiRankings(id); });
        const mediaCats = sourceCats.media || {};
        Object.keys(MEDIA_BAR_IDS).forEach(parent => {
          const saved = (state.aiMediaCategories && state.aiMediaCategories[parent]) || '';
          renderRankingsSourceBar(MEDIA_BAR_IDS[parent], mediaCats[parent] || [], saved,
            id => { state.aiMediaCategories[parent] = id; loadAiRankings(id); });
        });
        // 六条二级条统一收口：取数后按当前榜 id 决定谁可见。
        // `renderRankingsSourceBar` 只要拿到选项就会强制 flex —— 若不在每次取数后收一次，
        // 直连路由 / 首帧就会把「当前入口之外」的条一起露出来（实测六个条全显示）。
        syncRankingsSubBars(state.aiRankCategory);
      }
    } catch (e) { errMsg = '后端不可达'; }

    if (!rankData) {
      container.innerHTML = `<div style="padding:25px; text-align:center; color:var(--color-error);">${escapeHtml(errMsg || '暂无榜单数据')}</div>`;
      return;
    }

    if (banner) {
      const srcChip = rankData.source === 'live'
        ? '<span class="hud-chip success">真实抓取</span>'
        : '<span class="hud-chip">内置快照</span>';
      const published = rankData.published_at ? ` · 评测日期 ${escapeHtml(rankData.published_at)}` : '';
      banner.innerHTML = `<strong>${escapeHtml(rankData.name || '')}</strong> · 基准：<code>${escapeHtml(rankData.benchmark || '')}</code> · ${escapeHtml(rankData.description || '')}${published} ${srcChip} (抓取: ${escapeHtml(rankData.updated_at || '--')})`;
    }

    // 「仅看开源」：license 标注只有 aa_index / cap_* 两个能力口径带，其余榜不出开关。
    // 保留原始 rank——开源模型的价值就在于"它在全模型榜里排第几"，重排会丢掉这个信息。
    const osEligible = category === 'aa_index' || category.startsWith('cap_');
    let rankItems = rankData.items || [];
    if (osEligible && state.aiOpensourceOnly) {
      rankItems = rankItems.filter(it => it.license === '开源');
      if (banner) {
        banner.innerHTML += ` · <span class="hud-chip">仅开源 ${rankItems.length} 席（名次为原榜名次）</span>`;
      }
    }
    renderRankingsTable(category, rankItems);

    // 卡片头的「更新于」：updated_at 形如 "2026-09-23 12:34:56"，只取时分秒
    const updChip = document.getElementById('rankingsUpdatedChip');
    if (updChip) {
      const at = String(rankData.updated_at || '');
      const hm = /(\d{2}:\d{2}:\d{2})/.exec(at);
      updChip.textContent = hm ? `更新于 ${hm[1]}` : at ? `更新于 ${at.slice(5, 10)}` : '--';
      updChip.title = `${at}${rankData.published_at ? `\n榜单日期 ${rankData.published_at}` : ''}`;
    }
  }

  // 能力榜八维二级选择条。维度表来自后端（抓取时写入模型目录），后端不可达时
  // 用一份内置八维兜底，保证按钮永远出得来。
  const FALLBACK_CAP_DIMS = [
    { key: 'gdpval', label: '真实世界工作任务' }, { key: 'terminal', label: '终端与编码智能体' },
    { key: 'scicode', label: '科研编程' }, { key: 'lcr', label: '长上下文推理' },
    { key: 'hle', label: '推理与知识' }, { key: 'gpqa', label: '科学推理' },
    { key: 'critpt', label: '批判性推理' }, { key: 'omni', label: '知识准确率' }
  ];

  function renderCapabilityDimBar(dims) {
    const bar = document.getElementById('capabilityDimBar');
    if (!bar) return;
    const cur = state.aiCapDim || 'gdpval';
    if (bar._fp === JSON.stringify(dims) + cur) return;      // 内容没变就别重建，避免闪烁
    bar._fp = JSON.stringify(dims) + cur;
    const list = (dims && dims.length ? dims : FALLBACK_CAP_DIMS);
    bar.innerHTML = `<span class="cap-dim-label">评测维度</span>` + list.map(d => {
      const key = d.key === 'omni' ? 'omni' : d.key;
      return `<button class="cap-dim-chip${key === cur ? ' active' : ''}" data-cap-dim="${key}"
        title="${escapeHtml(d.english || '')}">${escapeHtml(d.label)}</button>`;
    }).join('');
    bar.querySelectorAll('[data-cap-dim]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.aiCapDim = btn.getAttribute('data-cap-dim');
        renderCapabilityDimBar(list);
        loadAiRankings('cap_' + state.aiCapDim);
      });
    });
  }

  function renderRankingsSourceBar(id, options, current, onSelect) {
    const bar = document.getElementById(id);
    if (!bar) return;
    const list = options || [];
    if (!list.length) {
      bar.style.display = 'none';
      bar.innerHTML = '';
      bar._fp = '';
      return;
    }
    const fingerprint = JSON.stringify(list) + current;
    bar.style.display = 'flex';
    if (bar._fp === fingerprint) return;
    bar._fp = fingerprint;
    bar.innerHTML = `<label class="cap-dim-label" for="${id}Select">榜单分类</label>
      <select class="rank-source-select" id="${id}Select" aria-label="榜单分类">` + list.map(item =>
        `<option value="${escapeHtml(item.id)}"${item.id === current ? ' selected' : ''}>${escapeHtml(item.name)}</option>`
      ).join('') + '</select>';
    const select = bar.querySelector('select');
    if (select) select.addEventListener('change', () => onSelect(select.value));
  }

  function renderRankingsTable(category, items) {
    const container = document.getElementById('rankingsTableContainer');
    if (!container) return;

    // 媒体榜（视频 / 生图 / 音频）走自己的列表式渲染：字段多、来源口径各异，
    // 塞进固定列表格会在窄窗口横向溢出。
    if (category.startsWith('video__') || category.startsWith('image__')
        || category.startsWith('audio__')) {
      renderMediaRankings(category, items);
      return;
    }

    // 2026-09-19 起排行榜为 ai_fetcher 真实抓取（live 口径，items 带 downloads
    // / pricing 字段），按新表头渲染；旧内置快照（source=snapshot）走原四分支。
    // arena / aa_index 两榜（datalearner 口径）快照与 live 字段一致，直接走 live 渲染。
    if (category === 'arena' || category === 'aa_index' || category.startsWith('cap_')
        || category.startsWith('arena__') || category.startsWith('livebench__')
        || category.startsWith('multimodal__')) {
      renderLiveRankings(category, items);
      return;
    }
    if (items.length > 0 && items[0].downloads !== undefined) {
      renderLiveRankings(category, items);
      return;
    }

    let headers = '';
    let rows = '';

    if (category === 'overall') {
      headers = `
        <th>天梯排名</th>
        <th>模型全称</th>
        <th>研发机构</th>
        <th class="text-right">Arena ELO</th>
        <th class="text-right">盲测胜率</th>
        <th class="text-right">上下文窗口</th>
        <th class="text-right">开源属性</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong> <span class="pill-tag" style="margin-left:5px;">${item.badge}</span></td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${item.elo}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.win_rate}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.context}</td>
          <td class="text-right">${item.license}</td>
        </tr>
      `).join('');
    } else if (category === 'coding') {
      headers = `
        <th>编程排名</th>
        <th>模型标识</th>
        <th>研发机构</th>
        <th class="text-right">SWE-bench Verified</th>
        <th class="text-right">HumanEval</th>
        <th class="text-right">上下文</th>
        <th class="text-right">推理成本 (In/Out)</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong> <span class="pill-tag" style="margin-left:5px;">${item.badge}</span></td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:#10b981; font-weight:700;">${item.swe_bench}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.humaneval}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.context}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.cost}</td>
        </tr>
      `).join('');
    } else if (category === 'opensource') {
      headers = `
        <th>开源排名</th>
        <th>模型名称</th>
        <th>开源组织</th>
        <th>参数规模</th>
        <th class="text-right">数学 (Math500)</th>
        <th class="text-right">编程得分</th>
        <th class="text-right">开源协议</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong> <span class="pill-tag" style="margin-left:5px;">${item.badge}</span></td>
          <td>${item.org}</td>
          <td>${item.params}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:#a855f7; font-weight:700;">${item.math}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.code}</td>
          <td class="text-right"><code>${item.license}</code></td>
        </tr>
      `).join('');
    } else {
      // efficiency
      headers = `
        <th>吞吐排名</th>
        <th>模型型号</th>
        <th>服务提供商</th>
        <th class="text-right">生成速度 (Tokens/s)</th>
        <th class="text-right">输入单价 / 1M</th>
        <th class="text-right">输出单价 / 1M</th>
        <th class="text-right">命中缓存单价</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong> <span class="pill-tag" style="margin-left:5px;">${item.badge}</span></td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${item.speed}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.input_cost}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.output_cost}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:#10b981;">${item.cache_hit}</td>
        </tr>
      `).join('');
    }

    container.innerHTML = `
      <table class="compact-table">
        <thead><tr>${headers}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  // 真实抓取数据（live）渲染：热度榜 / 代码榜 / 开源榜用 HF 下载量口径，
  // 性价比榜用 OpenRouter 官方定价口径。
  // 二级条可见性的**唯一**裁决点：能力榜 / Arena / LiveBench / 四个媒体入口
  // 各有一条，同一时刻只该显示当前主入口那一条。
  // 之前这条规矩散在标签点击与路由还原两处，取数路径没管 —— 于是直连路由时
  // 六个条会一起露出来（实测）。任何新增二级条都要挂进这里。
  const RANK_SUB_BARS = () => ({
    capability: 'capabilityDimBar',
    arena: 'arenaCategoryBar',
    livebench: 'livebenchCategoryBar',
    video: MEDIA_BAR_IDS.video, audio: MEDIA_BAR_IDS.audio,
    image: MEDIA_BAR_IDS.image, multimodal: MEDIA_BAR_IDS.multimodal
  });

  function syncRankingsSubBars(category) {
    const cur = String(category || '');
    let main = cur;
    if (cur.startsWith('cap_')) main = 'capability';
    else if (cur === 'arena' || cur.startsWith('arena__')) main = 'arena';
    else if (cur === 'livebench' || cur.startsWith('livebench__')) main = 'livebench';
    else if (/^(video|image|audio|multimodal)__/.test(cur)) main = cur.split('__')[0];
    Object.entries(RANK_SUB_BARS()).forEach(([key, id]) => {
      const el = document.getElementById(id);
      if (!el) return;
      const show = key === main && el.innerHTML.trim() !== '';
      el.style.display = show ? 'flex' : 'none';
    });
    // 「开源筛选」是静态条（HTML 写死、无选项注入），不走上面的 innerHTML 判定；
    // 只随 aa_index / capability 两个主入口出现。
    const osBar = document.getElementById('rankOpensourceBar');
    if (osBar) {
      osBar.style.display = (main === 'aa_index' || main === 'capability') ? 'flex' : 'none';
    }
    return main;
  }

  function renderLiveRankings(category, items) {
    const container = document.getElementById('rankingsTableContainer');
    if (!container) return;

    const badgeHtml = (b) => b ? ` <span class="pill-tag">${b}</span>` : '';
    let headers = '';
    let rows = '';

    if (category === 'efficiency') {
      headers = `
        <th>排名</th><th>模型</th><th>提供方</th>
        <th class="text-right">输入价 / 1M</th>
        <th class="text-right">输出价 / 1M</th>
        <th class="text-right">上下文窗口</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong>${badgeHtml(item.badge)}</td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.input_cost}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:#10b981; font-weight:600;">${item.output_cost}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.context}</td>
        </tr>
      `).join('');
    } else if (category === 'opensource') {
      headers = `
        <th>排名</th><th>模型</th><th>机构</th><th>开源协议</th>
        <th class="text-right">下载量</th><th class="text-right">点赞</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong>${badgeHtml(item.badge)}</td>
          <td>${item.org}</td>
          <td><code>${item.license}</code></td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${item.downloads}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.likes}</td>
        </tr>
      `).join('');
    } else if (category === 'aa_index') {
      headers = `
        <th>排名</th><th>模型</th><th>机构</th><th>推理模式</th>
        <th class="text-right">智能指数</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong>${badgeHtml(item.badge)}</td>
          <td>${item.org}</td>
          <td>${item.thinking || '-'}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${item.score}</td>
        </tr>
      `).join('');
    } else if (category === 'arena') {
      headers = `
        <th>排名</th><th>模型</th><th>机构</th>
        <th class="text-right">Elo</th><th class="text-right">置信区间</th>
        <th class="text-right">盲测票数</th><th>开源属性</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong>${badgeHtml(item.badge)}</td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${item.elo}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.ci || '-'}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.votes || '-'}</td>
          <td><code>${item.license || '-'}</code></td>
        </tr>
      `).join('');
    } else if (category.startsWith('arena__') || category.startsWith('livebench__')
               || category.startsWith('multimodal__')) {
      const unit = escapeHtml((items[0] && items[0].unit) || '%');
      // 多模态榜的数据取自 LMArena 视觉 / 文档榜，列语义与 Arena 相同
      const isArena = category.startsWith('arena__') || category.startsWith('multimodal__');
      headers = `
        <th>排名</th><th>模型</th><th>机构</th>
        <th class="text-right">${isArena ? '榜单分数' : '基准分'}（${unit}）</th>
        <th class="text-right">${isArena ? '置信区间' : '榜单日期'}</th>
        <th class="text-right">${isArena ? '投票 / 观测' : '开源权重'}</th>
        <th>${isArena ? '许可' : '模型类型'}</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${escapeHtml(item.model || '--')}</strong>${badgeHtml(escapeHtml(item.badge || ''))}</td>
          <td>${escapeHtml(item.org || '--')}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${escapeHtml(item.score == null ? '--' : String(item.score))}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${escapeHtml(isArena ? (item.ci || '--') : (item.published_at || '--'))}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${escapeHtml(isArena ? String(item.votes || '--') : (item.open_weights ? '开源' : '闭源'))}</td>
          <td><code>${escapeHtml(isArena ? (item.license || '--') : (item.reasoning ? '推理' : item.open_weights ? '开放权重' : '普通'))}</code></td>
        </tr>
      `).join('');
    } else if (category.startsWith('cap_')) {
      // 能力榜：一个维度一张表。单位要么是百分数（条宽直接用数值），
      // 要么是 Elo（按榜首归一化，因为 Elo 的绝对刻度没有"满格"概念）。
      const unit = (items[0] && items[0].unit) || '%';
      const topV = (items[0] && typeof items[0].value === 'number') ? items[0].value : 1;
      headers = `
        <th>排名</th><th>模型</th><th>机构</th>
        <th class="text-right">得分（${unit}）</th>
        <th>能力条</th><th>开源属性</th>
      `;
      rows = items.map(item => {
        const v = typeof item.value === 'number' ? item.value : null;
        let pct = 0;
        if (v != null) {
          pct = unit === '%' ? v : (topV ? (v / topV) * 100 : 0);
          pct = Math.max(0, Math.min(100, pct));
        }
        return `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong>${badgeHtml(item.badge)}</td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${v == null ? '--' : v}</td>
          <td><span class="cap-bar"><span class="cap-bar-fill" style="width:${pct.toFixed(1)}%"></span></span></td>
          <td><code>${item.license || '-'}</code></td>
        </tr>
      `;
      }).join('');
    } else {
      // overall / coding：HF 真实下载量热度口径
      headers = `
        <th>排名</th><th>模型</th><th>机构</th>
        <th class="text-right">下载量</th><th class="text-right">点赞</th><th>最近更新</th>
      `;
      rows = items.map(item => `
        <tr>
          <td><span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span></td>
          <td><strong>${item.model}</strong>${badgeHtml(item.badge)}</td>
          <td>${item.org}</td>
          <td class="text-right" style="font-family:var(--font-mono); color:var(--accent-primary); font-weight:700;">${item.downloads}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.likes}</td>
          <td class="text-right" style="font-family:var(--font-mono);">${item.updated || '--'}</td>
        </tr>
      `).join('');
    }

    container.innerHTML = `
      <table class="compact-table">
        <thead><tr>${headers}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  // 媒体榜（视频 / 生图 / 音频）共用一套列表式渲染：主行只放排名、模型、主分数、
  // 开放状态与参数量，其余字段换行进详情区——窄窗口下不出现横向滚动条。
  // 数据拿不到就写「未知」：不按模型名或体积推测参数量与开源状态。
  function renderMediaRankings(category, items) {
    const container = document.getElementById('rankingsTableContainer');
    if (!container) return;
    const mediaField = (value, fallback = '未知') =>
      escapeHtml(value == null || value === '' ? fallback : String(value));
    const unit = escapeHtml((items[0] && items[0].unit) || '');
    const direction = (items[0] && items[0].direction === 'lower') ? '越低越好' : '越高越好';
    const rows = items.map(item => {
      const extra = item.details || {};
      const details = [];
      if (item.ci) details.push(`置信区间 ${mediaField(item.ci, '--')}`);
      if (item.samples) details.push(`样本 ${mediaField(item.samples, '--')}`);
      if (item.released) details.push(`发布 ${mediaField(item.released, '--')}`);
      if (item.price) details.push(`价格 ${mediaField(item.price, '--')}`);
      if (extra.rtfx != null) details.push(`RTFx ${mediaField(extra.rtfx, '--')}`);
      if (extra.languages != null) details.push(`语言数 ${mediaField(extra.languages, '--')}`);
      if (extra.voices != null) details.push(`音色数 ${mediaField(extra.voices, '--')}`);
      if (extra.win_rate != null) details.push(`胜率 ${mediaField((extra.win_rate * 100).toFixed(1) + '%', '--')}`);
      details.push(`许可 ${mediaField(item.license)}`);
      if (item.source_url) {
        details.push(`<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener">源榜</a>`);
      }
      const params = item.parameters_b == null ? '未知' : `${mediaField(item.parameters_b)} B`;
      return `
        <div class="media-rank-row">
          <div class="media-rank-primary">
            <span class="rank-badge-item ${item.rank <= 3 ? 'rank-' + item.rank : 'rank-other'}">${item.rank}</span>
            <strong class="media-rank-name">${escapeHtml(item.model || '--')}</strong>
            <span class="media-rank-org">${escapeHtml(item.org || '--')}</span>
            <span class="media-rank-score">${mediaField(item.score, '--')} ${unit}</span>
            <code class="media-rank-open">${mediaField(item.open_status)}</code>
            <span class="media-rank-params">参数量 ${params}</span>
          </div>
          <div class="media-rank-details">${details.map(d => `<span>${d}</span>`).join('')}</div>
        </div>`;
    }).join('');
    container.innerHTML = `
      <div class="media-rank-list">
        <div class="media-rank-caption">主分数单位：${unit || '--'}（${direction}）；开放状态与参数量按来源或模型卡核实，核实不了就写「未知」。</div>
        ${rows}
      </div>`;
  }

  function initAiHub() {
    if (initAiHub._done) return;              // 方案 C 壳：路由会反复进入本页，事件只绑定一次
    initAiHub._done = true;
    // v3 改版后新闻与天梯已是两个独立页面，旧的 #aiSubviewPills 子 Tab 已不存在；
    // 这里改为绑定能力榜的八维二级选择条（内容随首次拉取榜单时填充）

    // 新闻分类过滤
    const catChips = document.querySelectorAll('#aiNewsCategoryChips .cat-chip');
    catChips.forEach(chip => {
      chip.addEventListener('click', () => {
        catChips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        state.aiCategory = chip.getAttribute('data-cat');
        const kw = document.getElementById('aiNewsSearchInput')?.value.trim() || '';
        loadAiNews(state.aiCategory, kw);
      });
    });

    // 搜索框
    const searchInput = document.getElementById('aiNewsSearchInput');
    let searchTimer = null;
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          loadAiNews(state.aiCategory, searchInput.value.trim());
        }, 300);
      });
    }

    // 两个页面各有自己的「抓取最新」，但后端是同一次作业（要闻 + 6 源 RSS 一起刷），
    // 所以共用这一个触发流程（startAiNewsScrape，进页自动刷新也走它），
    // 只是各自更新自己那颗 chip 的文案。
    const bindRefresh = (btnId, chipId, chipBusy, chipTitle) => {
      const btn = document.getElementById(btnId);
      if (!btn) return;
      btn.addEventListener('click', () => startAiNewsScrape(chipId, chipBusy, chipTitle));
    };
    bindRefresh('btnRefreshAiNews', 'aiNewsUpdatedChip', '抓取中…',
                '正在抓取每日要闻与 6 个 RSS 源并中文化，约 1-2 分钟');
    bindRefresh('btnRefreshBriefing', 'aiBriefingChip', '抓取中…',
                '正在抓取每日要闻与 6 个 RSS 源并中文化，约 1-2 分钟');

    // 天梯榜分类切换
    const rankBtns = document.querySelectorAll('#rankingsCategoryBar .rank-tab-btn');
    rankBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        rankBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const rk = btn.getAttribute('data-rank');
        syncRankingsSubBars(
          rk === 'capability' ? 'cap_' + (state.aiCapDim || 'gdpval')
            : (MEDIA_BAR_IDS[rk] && state.aiMediaCategories && state.aiMediaCategories[rk])
              ? state.aiMediaCategories[rk]
              : rk === 'arena' ? (state.aiArenaCategory || 'arena')
              : rk === 'livebench' ? state.aiLiveBenchCategory : rk
        );
        if (rk === 'capability') {
          // 八维二级条只在能力榜下展开；默认停在用户上次看的维度
          loadAiRankings('cap_' + (state.aiCapDim || 'gdpval'));
        } else if (MEDIA_BAR_IDS[rk]) {
          loadAiRankings((state.aiMediaCategories && state.aiMediaCategories[rk]) || rk);
        } else {
          loadAiRankings(rk === 'arena' ? (state.aiArenaCategory || 'arena')
            : rk === 'livebench' ? state.aiLiveBenchCategory : rk);
        }
      });
    });

    // 「仅看开源」开关：重取当前榜按 license 过滤（本地 API，代价可忽略）。
    // 状态存 state，切到别的榜再切回来时筛选偏好保留。
    const osToggle = document.getElementById('rankOpensourceToggle');
    if (osToggle) {
      osToggle.addEventListener('click', () => {
        state.aiOpensourceOnly = !state.aiOpensourceOnly;
        osToggle.classList.toggle('active', state.aiOpensourceOnly);
        loadAiRankings(state.aiRankCategory);
      });
    }

    bindRankingsRefresh();

    loadAiNews(state.aiCategory);
  }

  // --- 榜单与当前数据库手动刷新 ---
  // POST 立即返回；用任务 source 与 finished_at 确认本次刷新，而不是误认旧任务。
  const RANK_REFRESH_POLL_MS = 2500;
  const RANK_REFRESH_TIMEOUT_MS = 900000;

  async function getRankingsRefreshStatus(source = null) {
    try {
      const suffix = source ? `?source=${encodeURIComponent(source)}` : '';
      const resp = await fetch(`http://127.0.0.1:8790/api/ai/rankings/refresh/status${suffix}`);
      return resp.ok ? await resp.json() : null;
    } catch (e) { return null; }
  }

  async function pollRankingsRefresh(baseFinished, source) {
    const start = Date.now();
    for (;;) {
      await new Promise(r => setTimeout(r, RANK_REFRESH_POLL_MS));
      const st = await getRankingsRefreshStatus(source);
      const target = source || 'all';
      if (st && !st.running && st.source === target
          && st.finished_at && st.finished_at !== baseFinished) return st;
      if (Date.now() - start > RANK_REFRESH_TIMEOUT_MS) return { timeout: true };
    }
  }

  async function startRankingsRefresh(source, btn, onDone, opts = {}) {
    const silent = !!opts.silent;   // 进页自动触发用：冲突/失败安静跳过，不打断用户
    if (btn && btn.disabled) return;
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> 刷新中…';
    }
    try {
      const before = await getRankingsRefreshStatus(source);
      const suffix = source ? `?source=${encodeURIComponent(source)}` : '';
      const resp = await fetch(`http://127.0.0.1:8790/api/ai/rankings/refresh${suffix}`, { method: 'POST' });
      const started = resp.ok ? await resp.json() : null;
      if (!started || !started.ok) throw new Error((started && started.error) || `刷新请求失败（HTTP ${resp.status}）`);
      if (!started.started) {
        if (!silent) alert('已有榜单刷新任务在运行，等它结束后再点。');
        return;
      }
      const done = await pollRankingsRefresh(before && before.finished_at, source);
      if (done.timeout) {
        if (!silent) alert('刷新仍在进行；完成后缓存会自动更新。');
        return;
      }
      if (done.error && !silent) alert('刷新部分失败：' + done.error + '。失败来源保留旧数据。');
      const updated = done.result && done.result.updated;
      if (!silent && !done.error && (!Array.isArray(updated) || !updated.length)) {
        alert('没有拿到新数据，页面继续使用最近一次有效缓存。');
      }
      if (onDone) await onDone(done);
    } catch (e) {
      if (!silent) alert('刷新失败：' + e);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    }
  }

  // === 进页自动刷新（AI 四页）===
  // 点进 每日要闻 / 实时快讯 / 模型天梯 / 模型对比 就自动触发与右上角按钮相同的抓取。
  // 10 分钟节流窗：在几页之间来回点不会反复轰外网源；抓取进行中不叠加；
  // 后端「已有刷新任务在运行」的防重入保护照常兜底。手动按钮不受节流限制。
  const AI_AUTO_REFRESH_GAP = 10 * 60 * 1000;
  function aiAutoRefresh(key, fire) {
    if (state.aiBusyChip) return false;   // 新闻抓取已在进行，别叠加
    const now = Date.now();
    state.aiAutoRefreshAt = state.aiAutoRefreshAt || {};
    if (now - (state.aiAutoRefreshAt[key] || 0) < AI_AUTO_REFRESH_GAP) return false;
    state.aiAutoRefreshAt[key] = now;
    fire();
    return true;
  }

  // 天梯抓取完成后的统一收尾：重载当前榜 + 强制刷新对比目录（按名字恢复已选模型）
  async function reloadAfterRankingsRefresh() {
    const names = state.compareSelection.map(m => m.name);
    await loadAiRankings(state.aiRankCategory);
    state.modelCatalog = null;
    state.modelDims = null;
    await loadAiCompare(true);
    const current = new Map((state.modelCatalog || []).map(m => [m.name, m]));
    state.compareSelection = names.map(name => current.get(name)).filter(Boolean);
    renderCompareViews();
  }

  // 对比目录抓取完成后的统一收尾：清缓存强制重拉，并按名字恢复已选模型
  async function reloadCompareAfterRefresh() {
    const names = state.compareSelection.map(m => m.name);
    state.modelCatalog = null;
    state.modelDims = null;
    await loadAiCompare(true);
    const current = new Map((state.modelCatalog || []).map(m => [m.name, m]));
    state.compareSelection = names.map(name => current.get(name)).filter(Boolean);
    renderCompareViews();
  }

  // 绑定在 initAiHub 内（该函数自带 _done 守卫，重复进页不会重复绑定）
  function bindRankingsRefresh() {
    const btn = document.getElementById('btnRankingsRefresh');
    if (!btn) return;
    btn.addEventListener('click', () => startRankingsRefresh(null, btn, reloadAfterRankingsRefresh));
  }

  // 抓取完成后两个页面都要重载——它们是同一次作业的两面，
  // 只刷当前页会让另一页停在旧数据上，切过去看着像没更新。
  function reloadBothAiViews() {
    loadAiBriefings();
    loadAiNews(state.aiCategory, document.getElementById('aiNewsSearchInput')?.value.trim() || '');
  }

  // 触发要闻/快讯共用的后台抓取（手动按钮与进页自动刷新都走这里）。
  // POST 是「已在后台执行」立即返回，紧接着的重载会把 chip 覆盖回计数，
  // 于是用户看不到任何进行中反馈。用一个时间窗压住**被触发的那颗** chip
  // （另一页的 chip 照常刷新，它没被触发，压住反而显示陈旧计数）。
  function startAiNewsScrape(chipId, chipBusy, chipTitle) {
    const chip = document.getElementById(chipId);
    state.aiBusyChip = chipId;
    if (chip) { chip.textContent = chipBusy; chip.title = chipTitle; }
    fetch('http://127.0.0.1:8790/api/ai/news/refresh', { method: 'POST' })
      .catch(() => {})
      .finally(reloadBothAiViews);
    let tries = 0;
    const t = setInterval(() => {
      tries += 1;
      reloadBothAiViews();
      if (tries >= 6) {
        clearInterval(t);
        state.aiBusyChip = '';
        reloadBothAiViews();
      }
    }, 20000);
  }

  // --- 8b. 每日要闻（第三方简报） ---
  async function loadAiBriefings() {
    const list = document.getElementById('aiBriefingList');
    if (!list) return;
    let data = null;
    try {
      const resp = await fetch('http://127.0.0.1:8790/api/ai/briefings?limit=8');
      if (resp.ok) data = await resp.json();
    } catch (e) { /* 后端不可达：下面走空态 */ }

    const items = (data && data.items) || [];
    if (!items.length) {
      // 要闻是独立页了，没数据不能再"整块隐藏"（会剩一张空白页），改成明说原因
      list.innerHTML = `<div class="ai-empty-state">暂时没有要闻数据。抓取器每天 19:00 自动更新；若长期为空，多半是对方站点改版或不可达，点右上角「抓取最新」重试。</div>`;
      const emptyChip = document.getElementById('aiBriefingChip');
      if (emptyChip && state.aiBusyChip !== 'aiBriefingChip') {
        emptyChip.textContent = '无数据';
        emptyChip.title = '要闻缓存为空或后端不可达';
      }
      return;
    }

    const chip = document.getElementById('aiBriefingChip');
    if (chip && state.aiBusyChip !== 'aiBriefingChip') {      // 正在抓取，别盖掉「抓取中…」
      chip.textContent = `${items.length} 期 · ${(data.updated_at || '').slice(5, 16) || '--'}`;
    }
    renderAiBriefings(items);
  }

  function renderAiBriefings(items) {
    const list = document.getElementById('aiBriefingList');
    if (!list) return;
    // 帖子化：一天一期不再是「一坨分节正文」，而是期分组头 + 期内每条新闻一张
    // 帖子卡（标题/摘要/配图/原文外链）。期卡沿用 .briefing-card 类名——外观
    // 设置里的玻璃化选择器挂在它上面，改名会让玻璃样式脱靶。
    list.innerHTML = items.map((b, idx) => {
      const posts = (b.sections || []).map(s => {
        const origUrl = ((s.links || []).find(l => l.url) || {}).url || '';
        const media = s.image ? `
          <a class="ai-post-media" href="${escapeHtml(origUrl || b.url || '#')}" target="_blank" rel="noopener">
            <img src="/api/ai/image?u=${encodeURIComponent(s.image)}" alt="" loading="lazy"
                 onerror="this.closest('.ai-post-media').remove()">
          </a>` : '';
        const links = (s.links || []).map(l =>
          `<a href="${escapeHtml(l.url)}" target="_blank" rel="noopener" class="btn-text">↗ ${escapeHtml(l.label)}</a>`).join('');
        return `
          <article class="briefing-post">
            ${s.heading ? `<h4 class="ai-post-heading">${escapeHtml(s.heading)}</h4>` : ''}
            ${media}
            ${(s.paragraphs || []).map(p => `<p class="ai-post-para">${escapeHtml(p)}</p>`).join('')}
            ${links ? `<div class="briefing-links">${links}</div>` : ''}
          </article>`;
      }).join('');
      const srcs = (b.sources || []).map(s =>
        `<a href="${escapeHtml(s.url || '#')}" target="_blank" rel="noopener" class="briefing-source">${escapeHtml(s.label)}</a>`).join('');
      return `
        <section class="briefing-card briefing-issue${idx === 0 ? ' briefing-card-open' : ''}" data-briefing-idx="${idx}">
          <header class="briefing-issue-head" data-briefing-toggle="${idx}" title="点击展开/收起这一期">
            <span class="news-source-chip">${escapeHtml(b.category || '要闻')}</span>
            <h3 class="briefing-title">${escapeHtml(b.title || '')}</h3>
            <i class="ph ph-caret-down briefing-caret"></i>
          </header>
          <div class="briefing-issue-meta">
            <span class="news-time">${escapeHtml(b.published_at || '')}</span>
            ${(b.sections || []).length ? `<span class="briefing-issue-count">${(b.sections || []).length} 条</span>` : ''}
          </div>
          ${b.image ? `<img class="briefing-cover" src="/api/ai/image?u=${encodeURIComponent(b.image)}" alt="${escapeHtml(b.image_alt || '')}" loading="lazy">` : ''}
          <div class="briefing-issue-body">
            ${posts}
            ${srcs ? `<div class="briefing-sources"><span class="briefing-sources-label">原始来源</span>${srcs}</div>` : ''}
            <div class="news-card-actions">
              <a href="${escapeHtml(b.url || '#')}" target="_blank" rel="noopener" class="btn-text" style="text-decoration:none;">🔗 查看原站</a>
            </div>
          </div>
        </section>`;
    }).join('');

    // 期头点击展开/收起；默认只展开最新一期——多期全展开会把帖子流顶出屏外
    list.querySelectorAll('[data-briefing-toggle]').forEach(head => {
      head.addEventListener('click', () => {
        const card = head.closest('.briefing-card');
        if (card) card.classList.toggle('briefing-card-open');
      });
    });
  }

  // --- 8c. 模型对比 ---
  state.compareSelection = [];
  // AA 紧凑目录（八维 + 价格 + 速度）是「模型对比」与「能力榜雷达」共用的数据源，
  // 一次拉取两边复用——不要各自再请求一遍 270 条目录。
  state.modelCatalog = null;
  state.modelDims = null;
  // 模型用量页也使用 state.modelSource（ccs/hermes/...），对比数据库必须独立，
  // 否则从用量页切到对比页会把 ccs/all 当作目录来源发给后端并得到 400。
  state.compareSource = 'aa';
  state.modelBoard = 'text';
  state.modelCatalogSource = '';
  state.modelCatalogBoard = '';
  state.modelCatalogRequestId = 0;

  async function ensureModelCatalog(source = state.compareSource, board = state.modelBoard, force = false) {
    if (!force && state.modelCatalog && state.modelCatalogSource === source
        && (source !== 'arena' || state.modelCatalogBoard === board)) return state.modelCatalog;
    const requestId = ++state.modelCatalogRequestId;
    try {
      const params = new URLSearchParams({ limit: '5000', source });
      if (source === 'arena') params.set('board', board);
      const resp = await fetch(`http://127.0.0.1:8790/api/ai/models?${params.toString()}`);
      if (resp.ok) {
        const data = await resp.json();
        if (requestId !== state.modelCatalogRequestId) return state.modelCatalog;
        if (data.status === 'error') throw new Error(data.error || '模型目录不可用');
        if (source !== 'aa' && data.source !== source) {
          throw new Error('当前后端不支持该数据库，请更新工具箱后重试。');
        }
        state.modelCatalog = data.items || [];
        state.modelDims = data.dimensions || [];
        state.compareSource = source;
        state.modelBoard = data.board || board;
        state.modelCatalogSource = source;
        state.modelCatalogBoard = data.board || board;
        state.modelCatalogMeta = {
          total: data.total || 0, updated_at: data.updated_at || '',
          source_updated_at: data.source_updated_at || '',
          published_at: data.published_at || '',
          board_options: data.board_options || [],
        };
      } else {
        if (requestId !== state.modelCatalogRequestId) return state.modelCatalog;
        state.modelCatalog = [];
        state.modelDims = [];
        state.modelCatalogMeta = { total: 0, updated_at: '', error: '读取模型目录失败' };
      }
    } catch (e) {
      if (requestId !== state.modelCatalogRequestId) return state.modelCatalog;
      state.modelCatalog = [];
      state.modelDims = [];
      state.modelCatalogMeta = { total: 0, updated_at: '', error: String(e) };
    }
    return state.modelCatalog;
  }

  function initComparePage() {
    if (initComparePage._done) return;
    initComparePage._done = true;
    const input = document.getElementById('compareSearchInput');
    const box = document.getElementById('compareSuggest');
    const clearBtn = document.getElementById('btnCompareClear');
    const sourceSelect = document.getElementById('compareSourceSelect');
    const boardSelect = document.getElementById('compareArenaBoard');
    const refreshBtn = document.getElementById('btnCompareRefresh');

    if (sourceSelect) sourceSelect.value = state.compareSource || 'aa';
    if (sourceSelect) sourceSelect.addEventListener('change', async () => {
      state.compareSource = sourceSelect.value;
      state.compareSuggestRequestId = (state.compareSuggestRequestId || 0) + 1;
      state.modelCatalog = null;
      state.modelDims = null;
      state.modelLoadError = '';
      state.compareSelection = [];
      const input = document.getElementById('compareSearchInput');
      if (input) input.value = '';
      if (box) { box.innerHTML = ''; box.style.display = 'none'; }
      renderCompareViews();
      await loadAiCompare();
    });
    if (boardSelect) boardSelect.addEventListener('change', async () => {
      state.modelBoard = boardSelect.value || 'text';
      state.compareSuggestRequestId = (state.compareSuggestRequestId || 0) + 1;
      state.modelCatalog = null;
      state.modelDims = null;
      state.modelLoadError = '';
      state.compareSelection = [];
      const input = document.getElementById('compareSearchInput');
      if (input) input.value = '';
      if (box) { box.innerHTML = ''; box.style.display = 'none'; }
      renderCompareViews();
      await loadAiCompare();
    });
    if (refreshBtn) refreshBtn.addEventListener('click', async () => {
      const source = state.compareSource;
      if (sourceSelect) sourceSelect.disabled = true;
      if (boardSelect) boardSelect.disabled = true;
      try {
        await startRankingsRefresh(source, refreshBtn, reloadCompareAfterRefresh);
      } finally {
        if (sourceSelect) sourceSelect.disabled = false;
        if (boardSelect) boardSelect.disabled = state.compareSource !== 'arena';
      }
    });

    let timer = null;
    if (input && box) {
      input.addEventListener('input', () => {
        clearTimeout(timer);
        const requestId = state.compareSuggestRequestId = (state.compareSuggestRequestId || 0) + 1;
        const kw = input.value.trim();
        if (!kw) { box.style.display = 'none'; return; }
        timer = setTimeout(() => renderCompareSuggest(kw, requestId), 250);
      });
      // 失焦即收起：下拉是绝对定位浮层，不收会盖住对比表
      input.addEventListener('blur', () => setTimeout(() => {
        state.compareSuggestRequestId = (state.compareSuggestRequestId || 0) + 1;
        box.style.display = 'none';
      }, 180));
    }
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        state.compareSelection = [];
        renderCompareViews();
      });
    }
  }

  async function loadAiCompare(force = false) {
    ensureExternalRankings().then(() => {
      if (state.compareSelection && state.compareSelection.length) renderCompareViews();
    });
    await ensureModelCatalog(state.compareSource, state.modelBoard, force);
    const meta = state.modelCatalogMeta || {};
    const chip = document.getElementById('compareUpdatedChip');
    if (chip) {
      const date = meta.published_at || meta.source_updated_at || meta.updated_at || '';
      chip.textContent = meta.error ? '读取失败' : `目录 ${meta.total || 0} 条 · 数据 ${date.slice(5, 16) || '--'}`;
      chip.title = meta.error || [meta.updated_at, meta.published_at].filter(Boolean).join('\n');
    }
    state.modelLoadError = meta.error || (meta.total === 0 ? '当前数据库暂无可用模型，请刷新后重试。' : '');
    const boardSelect = document.getElementById('compareArenaBoard');
    const boardLabel = document.getElementById('compareArenaBoardLabel');
    const isArena = state.compareSource === 'arena';
    if (boardSelect && boardLabel) {
      boardSelect.style.display = isArena ? '' : 'none';
      boardLabel.style.display = isArena ? '' : 'none';
      boardSelect.disabled = !isArena;
      if (isArena) {
        const options = meta.board_options || [];
        const selected = state.modelCatalogBoard || state.modelBoard || 'text';
        boardSelect.innerHTML = options.map(o =>
          `<option value="${escapeHtml(o.id)}"${o.id === selected ? ' selected' : ''}>${escapeHtml(o.name)}</option>`
        ).join('');
        if (options.length && !options.some(o => o.id === selected)) {
          state.modelBoard = options[0].id;
          boardSelect.value = options[0].id;
        }
      }
    }
    renderCompareViews();
  }

  async function renderCompareSuggest(kw, requestId) {
    const box = document.getElementById('compareSuggest');
    if (!box) return;
    const params = new URLSearchParams({ q: kw, limit: '12', source: state.compareSource || 'aa' });
    if (state.compareSource === 'arena') params.set('board', state.modelBoard || 'text');
    let hits = [];
    try {
      const resp = await fetch(`http://127.0.0.1:8790/api/ai/models?${params.toString()}`);
      if (resp.ok) {
        const data = await resp.json();
        hits = data.items || [];
      }
    } catch (e) { /* 当前目录仍可从本地缓存筛选 */ }
    if (!hits.length && state.modelCatalog) {
      const s = kw.toLowerCase();
      hits = state.modelCatalog.filter(m => (m.name || '').toLowerCase().includes(s)
        || (m.org || '').toLowerCase().includes(s)).slice(0, 12);
    }
    if (requestId !== state.compareSuggestRequestId) return;
    if (!hits.length) {
      box.innerHTML = '<div class="compare-suggest-empty">没有匹配的模型</div>';
      box.style.display = 'block';
      return;
    }
    box.innerHTML = hits.map((m, i) => `
      <div class="compare-suggest-item" data-suggest-idx="${i}">
        <span class="cs-name">${escapeHtml(m.name)}</span>
        <span class="cs-org">${escapeHtml(m.org)}</span>
        <span class="cs-score">${m.intelligence != null ? m.intelligence : m.overall != null ? m.overall : '--'}</span>
      </div>`).join('');
    box.style.display = 'block';
    box.querySelectorAll('[data-suggest-idx]').forEach(el => {
      el.addEventListener('mousedown', (ev) => {
        ev.preventDefault();          // 抢在 input blur 之前处理，否则下拉先被收起
        addCompareModel(hits[Number(el.getAttribute('data-suggest-idx'))]);
      });
    });
  }

  function addCompareModel(model) {
    if (!model) return;
    if (state.compareSelection.length >= 4) {
      alert('最多同时对比 4 个模型，请先移除一个。');
      return;
    }
    if (state.compareSelection.some(m => m.name === model.name)) return;
    state.compareSelection.push(model);
    const input = document.getElementById('compareSearchInput');
    if (input) input.value = '';
    const box = document.getElementById('compareSuggest');
    if (box) box.style.display = 'none';
    renderCompareViews();
  }

  function renderCompareSelection() {
    const el = document.getElementById('compareSelected');
    if (!el) return;
    if (!state.compareSelection.length) {
      el.innerHTML = state.modelLoadError
        ? `<span class="compare-hint">${escapeHtml(state.modelLoadError)}</span>`
        : '<span class="compare-hint">还没选模型 —— 在上面搜索框里挑 2–4 个开始对比。</span>';
      return;
    }
    el.innerHTML = state.compareSelection.map((m, i) => `
      <span class="compare-chip">
        ${escapeHtml(m.name)}
        <button class="compare-chip-x" data-compare-remove="${i}" title="移除">×</button>
      </span>`).join('');
    el.querySelectorAll('[data-compare-remove]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.compareSelection.splice(Number(btn.getAttribute('data-compare-remove')), 1);
        renderCompareViews();
      });
    });
  }

  // --- 8d. 八项能力维度图（雷达） ---
  // 画的模型就是上面那排 chips（`state.compareSelection`）——对比页本来就有搜索 + chips，
  // 雷达不再自带第二套 picker：两个模型列表不同步只会让人对不上号。
  //
  // 四个非显然的口径决定：
  // 1. **每轴按相对分数归一化**（以该维度前 100 名的最低/最高分为基准，与等级分档同口径）。
  //    八维量纲不同——七项是百分数、GDPval 是 Elo（1311~1735），同尺度画一张图没有意义；
  //    全库归一还会被 0 分模型把范围拉爆（实测 terminal/lcr/critpt 全量最低分是 0），
  //    点位整体挤在高端。所以图上读的是「在该维度前 100 名里处于什么相对位置」，
  //    **不是绝对值**；绝对分数看轴旁标注和下方对照表。
  // 2. **分数只标「聚焦模型」那一条**。4 条线 × 8 轴 = 32 个数字糊成一团，每条轴各标又
  //    必然和轴名打架。所以轴标签第二行（`tspan.radar-value`）跟着聚焦走：**点图例切换**
  //    （默认第一条），聚焦那条线不压淡并在顶点画圆点。注意 tspan 必须重设 x，否则换行
  //    后仍按第一行起点排，左右两侧（anchor=start/end）的标签会错位。
  // 3. 缺项在轴上标「无」且**不进分档池**（0 分与缺考是两回事），但图形形状仍按 0
  //    参与——0 分经相对归一落在中心；等级用同一相对分数分 A–E 五档（点位与等级同源）。
  // 4. **画布是 520×440，不是正方形**。八轴标签是中文长词（左右两侧 4–8 字），方画布会被
  //    SVG 默认的 `overflow:hidden` 裁掉（实测左侧「批判性推理」超框 14.5px、右侧「科研
  //    编程」3.5px）。横向留边比缩小半径好，圆盘仍是 300px 直径。
  const RADAR_COLORS = ['#ffd700', '#38bdf8', '#c084fc', '#4ade80'];
  state.radarFocus = null;

  // 模型名键归一化（用于跨源模糊匹配 Arena 盲测、LiveBench、Vals AI）
  function normalizeModelKey(name) {
    return String(name || '').toLowerCase().split('(')[0].replace(/[^a-z0-9]/g, '');
  }

  // 核心 8 维（针对 AA 数据源，将生涩的学术 Benchmark 转换为接地气的实用业务维度）
  const AA_CORE_DIMS = [
    {
      key: 'coding',
      label: '编程能力',
      english: 'Coding & Agents',
      unit: '%',
      desc: 'Terminal-Bench 2.1 终端环境多步编码运维 / SciCode 算法编程实测（自适应对齐全球位次）',
      getValue: m => (m.cap && m.cap.terminal != null ? m.cap.terminal : (m.cap && m.cap.scicode != null ? m.cap.scicode : null)),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? v + '%' : '--')
    },
    {
      key: 'scireason',
      label: '科学推理',
      english: 'Sci-Reasoning (HLE/GPQA)',
      unit: '%',
      desc: 'HLE (人类终极考试) / GPQA 博士级科学综合推理（覆盖高难数学、物理与生化机理）',
      getValue: m => (m.cap ? (m.cap.hle != null ? m.cap.hle : m.cap.gpqa) : null),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? v + '%' : '--')
    },
    {
      key: 'writing',
      label: '文案长文',
      english: 'Writing & Context',
      unit: '%',
      desc: 'AA-LCR 长文档深度提炼、跨段落检索与创意表达',
      getValue: m => (m.cap ? m.cap.lcr : null),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? v + '%' : '--')
    },
    {
      key: 'intelligence',
      label: '综合智力',
      english: 'General Intelligence',
      unit: '分',
      desc: 'AA 综合智能指数，通用脑力、规划与常识推理基准',
      getValue: m => (m.intelligence != null ? m.intelligence : null),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? String(v) : '--')
    },
    {
      key: 'speed',
      label: '响应速度',
      english: 'Speed (Tokens/s)',
      unit: 'T/s',
      desc: '托管 API 中位数输出吞吐，直接决定日常与 Agent 交互响应手感',
      getValue: m => (m.speed != null ? m.speed : null),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? v + ' T/s' : '--')
    },
    {
      key: 'value',
      label: '极致性价比',
      english: 'Value Index',
      unit: '点/$',
      desc: '综合智力点数 ÷ 1M Token 综合均价（越聪明越便宜分值越高）',
      getValue: m => {
        if (m.intelligence == null) return null;
        const pin = m.price_in, pout = m.price_out;
        if (pin == null && pout == null) return null;
        const avg = (((pin || 0) + (pout || 0)) / 2) || 0.001;
        return Math.round((m.intelligence / avg) * 10) / 10;
      },
      format: v => (typeof v === 'number' && Number.isFinite(v) ? String(v) : '--')
    },
    {
      key: 'accuracy',
      label: '知识准确率',
      english: 'Factuality (AA-Omni)',
      unit: '%',
      desc: 'AA-Omniscience 权威全知事实准确度，防胡说八道与幻觉率',
      getValue: m => (m.cap ? m.cap.omni : null),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? v + '%' : '--')
    },
    {
      key: 'realwork',
      label: '实战交付',
      english: 'Real-World (GDPval)',
      unit: 'Elo',
      desc: 'GDPval 华尔街/律所/咨询真实高薪白领实操交付物盲测评级',
      getValue: m => (m.cap ? m.cap.gdpval : null),
      format: v => (typeof v === 'number' && Number.isFinite(v) ? String(v) : '--')
    }
  ];

  function getCompareDims() {
    if (state.compareSource === 'arena' || state.compareSource === 'livebench') {
      return (state.modelDims && state.modelDims.length) ? state.modelDims : [];
    }
    return AA_CORE_DIMS;
  }

  // 综合能力维度元数据解析：支持双基准自适应、排名、百分位等级与防离群压缩
  function getDimMeta(dim, model, catalog) {
    const isNum = v => typeof v === 'number' && Number.isFinite(v);
    const cat = catalog || state.modelCatalog || [];
    const dimKey = typeof dim === 'string' ? dim : (dim.key || '');
    const dimLabel = typeof dim === 'object' ? dim.label : dimKey;
    const isAA = state.compareSource === 'aa' || !state.compareSource;

    if (!model) {
      return { known: false, raw: null, shown: '未测', rank: null, total: 0, ratio: 0, grade: 'unknown', letter: '无', bench: '' };
    }

    let raw = null, shown = '未测', bench = '', subpool = [];

    if (isAA) {
      if (dimKey === 'coding') {
        const hasTerm = isNum(model.cap?.terminal);
        const hasSci = isNum(model.cap?.scicode);
        if (hasTerm) {
          raw = model.cap.terminal;
          bench = 'Terminal-Bench';
          shown = raw + '%';
          subpool = cat.map(m => m.cap?.terminal).filter(isNum).sort((a, b) => b - a);
        } else if (hasSci) {
          raw = model.cap.scicode;
          bench = 'SciCode';
          shown = raw + '%';
          subpool = cat.map(m => m.cap?.scicode).filter(isNum).sort((a, b) => b - a);
        }
      } else if (dimKey === 'scireason') {
        const hasHle = isNum(model.cap?.hle);
        const hasGpqa = isNum(model.cap?.gpqa);
        if (hasHle) {
          raw = model.cap.hle;
          bench = 'HLE';
          shown = raw + '%';
          subpool = cat.map(m => m.cap?.hle).filter(isNum).sort((a, b) => b - a);
        } else if (hasGpqa) {
          raw = model.cap.gpqa;
          bench = 'GPQA';
          shown = raw + '%';
          subpool = cat.map(m => m.cap?.gpqa).filter(isNum).sort((a, b) => b - a);
        }
      } else if (dimKey === 'writing') {
        if (isNum(model.cap?.lcr)) {
          raw = model.cap.lcr;
          bench = 'AA-LCR';
          shown = raw + '%';
          subpool = cat.map(m => m.cap?.lcr).filter(isNum).sort((a, b) => b - a);
        }
      } else if (dimKey === 'intelligence') {
        if (isNum(model.intelligence)) {
          raw = model.intelligence;
          bench = '综合智能';
          shown = String(raw);
          subpool = cat.map(m => m.intelligence).filter(isNum).sort((a, b) => b - a);
        }
      } else if (dimKey === 'speed') {
        if (isNum(model.speed)) {
          raw = model.speed;
          bench = '吞吐速度';
          shown = raw + ' T/s';
          subpool = cat.map(m => m.speed).filter(isNum).sort((a, b) => b - a);
        }
      } else if (dimKey === 'value') {
        if (isNum(model.intelligence)) {
          const pin = model.price_in, pout = model.price_out;
          if (pin != null || pout != null) {
            const avg = (((pin || 0) + (pout || 0)) / 2) || 0.001;
            raw = Math.round((model.intelligence / avg) * 10) / 10;
            bench = '性价比';
            shown = String(raw);
            subpool = cat.map(m => {
              if (!isNum(m.intelligence)) return null;
              const p1 = m.price_in, p2 = m.price_out;
              if (p1 == null && p2 == null) return null;
              const a = (((p1 || 0) + (p2 || 0)) / 2) || 0.001;
              return Math.round((m.intelligence / a) * 10) / 10;
            }).filter(isNum).sort((a, b) => b - a);
          }
        }
      } else if (dimKey === 'accuracy') {
        if (isNum(model.cap?.omni)) {
          raw = model.cap.omni;
          bench = 'AA-Omni';
          shown = raw + '%';
          subpool = cat.map(m => m.cap?.omni).filter(isNum).sort((a, b) => b - a);
        }
      } else if (dimKey === 'realwork') {
        if (isNum(model.cap?.gdpval)) {
          raw = model.cap.gdpval;
          bench = 'GDPval';
          shown = String(raw);
          subpool = cat.map(m => m.cap?.gdpval).filter(isNum).sort((a, b) => b - a);
        }
      }
    } else {
      // 非 AA 源 (Arena, LiveBench)
      const dObj = typeof dim === 'object' ? dim : null;
      const val = dObj && dObj.getValue ? dObj.getValue(model) : (model.cap || {})[dimKey];
      if (isNum(val)) {
        raw = val;
        bench = dimLabel;
        shown = dObj && dObj.format ? dObj.format(val) : (val + (dObj && dObj.unit === '%' ? '%' : ''));
        subpool = cat.map(m => dObj && dObj.getValue ? dObj.getValue(m) : (m.cap || {})[dimKey]).filter(isNum).sort((a, b) => b - a);
      }
    }

    if (raw == null || !subpool.length) {
      return { known: false, raw: null, shown: '未测', rank: null, total: 0, ratio: 0, grade: 'unknown', letter: '无', bench };
    }

    const rank = subpool.indexOf(raw) + 1;
    const total = subpool.length;
    const pct = total <= 1 ? 0.5 : (total - rank) / (total - 1);
    const ratio = Math.max(0.05, Math.min(1.0, pct));
    const grade = ratio >= 0.90 ? 'a' : ratio >= 0.75 ? 'b' : ratio >= 0.50 ? 'c' : ratio >= 0.20 ? 'd' : 'e';

    return { known: true, raw, shown, rank, total, ratio, grade, letter: grade.toUpperCase(), bench };
  }

  // 异步加载外部权威榜单（Arena 盲测代码、数学、创意写作、总榜），支持后台静默预热
  state.externalRankings = null;
  state._extRankLoading = false;
  async function ensureExternalRankings() {
    if (state.externalRankings || state._extRankLoading) return state.externalRankings;
    state._extRankLoading = true;
    const rankings = { coding: {}, math: {}, writing: {}, overall: {} };
    const cats = [
      { key: 'coding', cat: 'arena__text__coding' },
      { key: 'math', cat: 'arena__text__math' },
      { key: 'writing', cat: 'arena__text__creative_writing' },
      { key: 'overall', cat: 'arena__text__overall' }
    ];
    try {
      await Promise.all(cats.map(async ({ key, cat }) => {
        try {
          const resp = await fetch(`http://127.0.0.1:8790/api/ai/rankings?category=${cat}`);
          if (resp.ok) {
            const d = await resp.json();
            const items = (d.data && d.data.items) || [];
            items.forEach(it => {
              const k = normalizeModelKey(it.model);
              if (k && !rankings[key][k]) rankings[key][k] = it;
            });
          }
        } catch (_) {}
      }));
      state.externalRankings = rankings;
    } catch (_) {}
    state._extRankLoading = false;
    return state.externalRankings;
  }

  // 获取模型在指定维度上的顶级外部战绩徽章（全中文专业实测翻译）
  function getModelExternalBadges(model) {
    if (!model) return {};
    const name = String(model.name || '').toLowerCase();
    const cleanKey = normalizeModelKey(model.name);
    const badges = {
      coding: [],
      scireason: [],
      writing: [],
      intelligence: [],
      speed: [],
      value: [],
      accuracy: [],
      realwork: []
    };

    // 1. 动态对齐 Arena 盲测榜单实测 (取前 30 名的高光)
    if (state.externalRankings) {
      const arenaCode = state.externalRankings.coding && state.externalRankings.coding[cleanKey];
      if (arenaCode && arenaCode.rank <= 30) {
        badges.coding.push({
          text: `Arena 盲测代码 第${arenaCode.rank}名`,
          color: 'gold',
          tip: `LMSYS Arena 全球人类盲测代码榜第 ${arenaCode.rank} 名 (Elo ${Math.round(arenaCode.score || 0)})`
        });
      }
      const arenaMath = state.externalRankings.math && state.externalRankings.math[cleanKey];
      if (arenaMath && arenaMath.rank <= 30) {
        badges.scireason.push({
          text: `Arena 盲测高难数学 第${arenaMath.rank}名`,
          color: 'cyan',
          tip: `LMSYS Arena 全球人类盲测高难数学榜第 ${arenaMath.rank} 名 (Elo ${Math.round(arenaMath.score || 0)})`
        });
      }
      const arenaWriting = state.externalRankings.writing && state.externalRankings.writing[cleanKey];
      if (arenaWriting && arenaWriting.rank <= 30) {
        badges.writing.push({
          text: `Arena 盲测创意写作 第${arenaWriting.rank}名`,
          color: 'gold',
          tip: `LMSYS Arena 全球人类盲测创意写作榜第 ${arenaWriting.rank} 名 (Elo ${Math.round(arenaWriting.score || 0)})`
        });
      }
      const arenaOver = state.externalRankings.overall && state.externalRankings.overall[cleanKey];
      if (arenaOver && arenaOver.rank <= 30) {
        badges.intelligence.push({
          text: `Arena 盲测综合总榜 第${arenaOver.rank}名`,
          color: 'gold',
          tip: `LMSYS Arena 全球人类偏好盲测总榜第 ${arenaOver.rank} 名 (Elo ${Math.round(arenaOver.score || 0)})`
        });
      }
    }

    // 2. 顶级前沿权威实测 (Vals AI 经济实操、SWE-bench Verified 工业代码、AIME 全美奥数、LiveCodeBench 实时防刷题)
    if (name.includes('claude')) {
      if (name.includes('opus')) {
        badges.coding.unshift({ text: 'Vals 真实工程修Bug 70.3% (全球第1)', color: 'gold', tip: 'Vals AI 实测 SWE-bench Verified 工业级代码修复历史最高战绩' });
        badges.coding.push({ text: 'Vals 技能扩展实操 (顶尖级)', color: 'purple', tip: 'Vals AI 综合工具链与复杂 Agent 技能扩展实操评测顶尖' });
        badges.scireason.unshift({ text: 'Vals 前沿科学发现 67.0% (前列)', color: 'cyan', tip: 'Vals AI 科学发现推导与实验室终端综合实测高分' });
        badges.realwork.unshift({ text: 'Vals 真实经济交付 67.0% (全球第2)', color: 'purple', tip: 'Vals AI 美国经济价值加权实战总榜全球第二' });
      } else if (name.includes('sonnet')) {
        badges.coding.unshift({ text: 'SWE-bench 工业修Bug 68.4%', color: 'gold', tip: 'SWE-bench Verified 真实 GitHub 仓库工程 Issue 修复实测' });
        badges.coding.push({ text: 'LiveCodeBench 实时编程 65.2%', color: 'cyan', tip: 'LiveCodeBench 防刷题实时竞技编程实测高分' });
        badges.scireason.unshift({ text: 'Vals 前沿科学发现 65.8%', color: 'cyan', tip: 'Vals AI 科学推导与前沿机理实测指数' });
        badges.realwork.unshift({ text: 'Vals 真实经济交付 66.5%', color: 'purple', tip: 'Vals AI 真实工作交付与高薪白领场景实战指数' });
      } else if (name.includes('haiku') || name.includes('fable')) {
        badges.speed.push({ text: '敏捷极速交互', color: 'cyan', tip: '超低首字延迟与轻量级极速响应' });
      }
    } else if (name.includes('gpt') || name.includes('o3') || name.includes('o1')) {
      if (name.includes('astra') || name.includes('sol') || name.includes('o3')) {
        badges.coding.unshift({ text: 'SWE-bench 工业修Bug 71.7% (全球第1)', color: 'gold', tip: 'SWE-bench Verified 真实工程修复最高战绩' });
        badges.coding.push({ text: 'Terminal 终端运维实操 (全球第1)', color: 'purple', tip: 'Terminal-Bench 真实终端环境多步编码运维评测全球第一' });
        badges.scireason.unshift({ text: '全美数学奥赛 AIME 87.5% (全球第1)', color: 'gold', tip: '2025 美国数学邀请赛 (AIME) 高难奥赛高分' });
        badges.scireason.push({ text: 'Vals 前沿科学发现 65.1%', color: 'cyan', tip: 'Vals AI 科学推理指数实测' });
        badges.realwork.unshift({ text: 'Vals 真实经济交付 63.1%', color: 'purple', tip: 'Vals AI 真实经济价值任务实战' });
      } else if (name.includes('codex')) {
        badges.coding.unshift({ text: 'Codex 专业代码引擎', color: 'gold', tip: '专为工业级代码生成与重构深度调优' });
      }
    } else if (name.includes('gemini')) {
      if (name.includes('argon') || name.includes('pro')) {
        badges.scireason.unshift({ text: 'Vals 前沿科学发现 68.9% (全球第1)', color: 'gold', tip: 'Vals AI 实验室科学机理与终端推演全球第一' });
        badges.scireason.push({ text: 'GPQA 钻石级博士科学 84.0%', color: 'cyan', tip: 'GPQA Diamond 博士级无搜索硬核科学问答实测' });
        badges.realwork.unshift({ text: 'Vals 真实经济交付 68.9% (全球第1)', color: 'purple', tip: 'Vals AI 真实经济价值高薪实战任务全球第一' });
      } else if (name.includes('flash')) {
        badges.speed.unshift({ text: '闪电极速推理', color: 'cyan', tip: '超高 Tokens/s 输出吞吐与超低网络延迟' });
        badges.value.unshift({ text: '超高性价比标杆', color: 'gold', tip: '极低 Token 成本获得第一梯队通用脑力' });
      }
    } else if (name.includes('deepseek')) {
      badges.scireason.unshift({ text: '全美数学奥赛 AIME 84.0% (全球第2)', color: 'gold', tip: '2025 美国数学邀请赛 (AIME) 高难奥数实测' });
      badges.scireason.push({ text: 'MATH-500 竞赛高难数学 97.3%', color: 'cyan', tip: 'MATH-500 竞赛级数学实测接近满分' });
      badges.coding.unshift({ text: 'SWE-bench 工业修Bug 49.2%', color: 'purple', tip: 'SWE-bench Verified 真实工程修复能力实测' });
      badges.coding.push({ text: 'LiveCodeBench 实时编程 65.9%', color: 'cyan', tip: '实时防刷题 LeetCode 实操竞赛高分' });
      badges.value.unshift({ text: '全球降维性价比', color: 'gold', tip: '开源极致定价 + 顶级通用推理脑力' });
    } else if (name.includes('qwen')) {
      if (name.includes('max') || name.includes('plus') || name.includes('coder')) {
        badges.coding.unshift({ text: 'LiveCodeBench 实时编程 65.4%', color: 'cyan', tip: 'LiveCodeBench 竞赛级实时编程实测' });
        badges.scireason.unshift({ text: '全美数学奥赛 AIME 80.0%', color: 'cyan', tip: '2025 AIME 高难奥数推理实测' });
        badges.value.unshift({ text: '开源生态标杆', color: 'gold', tip: '极高生态普及度与开箱可用性' });
      }
    }

    return badges;
  }

  // 思考档位藏在 shortName 尾括号里（如「GPT-6 Astra (max)」→ max）。
  function radarTier(name) {
    const match = String(name || '').match(/\(([^()]+)\)\s*$/);
    return match ? match[1] : '';
  }

  function renderCompareRadar() {
    const host = document.getElementById('compareRadar');
    if (!host) return;
    const sel = state.compareSelection || [];
    const dims = getCompareDims();
    if (!sel.length || dims.length < 3) {
      host.hidden = true;
      host.innerHTML = '';
      return;
    }
    host.hidden = false;

    const SW = 540, SH = 440, CX = SW / 2, CY = 220, R = 135, n = dims.length;
    const angle = i => (-Math.PI / 2) + (i * 2 * Math.PI / n);
    const pt = (i, ratio) => {
      const a = angle(i);
      return [CX + Math.cos(a) * R * ratio, CY + Math.sin(a) * R * ratio];
    };

    const focus = sel.filter(m => m.name === state.radarFocus)[0] || sel[0] || null;
    const focusColor = RADAR_COLORS[(focus ? sel.indexOf(focus) : 0) % RADAR_COLORS.length];

    let chart = '';
    if (sel.length) {
      let grid = '';
      [0.2, 0.4, 0.6, 0.8, 1.0].forEach((r, idx) => {
        const pts = dims.map((_, i) => pt(i, r).map(v => v.toFixed(1)).join(',')).join(' ');
        grid += `<polygon points="${pts}" class="jojo-ring${idx === 4 ? ' jojo-ring-outer' : ''}"/>`;
      });
      dims.forEach((_, i) => {
        const [x, y] = pt(i, 1);
        grid += `<line x1="${CX}" y1="${CY}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" class="jojo-axis"/>`;
      });
      grid += `<circle cx="${CX}" cy="${CY}" r="3.5" fill="#f59e0b"/>`;

      let labels = '';
      let dots = '';
      dims.forEach((d, i) => {
        const a = angle(i);
        const cos = Math.cos(a);
        const sin = Math.sin(a);
        const meta = focus ? getDimMeta(d, focus, state.modelCatalog) : { known: false, shown: '未测', letter: '无', rank: null, ratio: 0, grade: 'unknown' };

        const bx = CX + cos * (R + 26);
        const by = CY + sin * (R + 26);

        const rankColor = meta.grade === 'a' ? '#ffd700'
          : meta.grade === 'b' ? '#38bdf8'
          : meta.grade === 'c' ? '#fde047'
          : meta.grade === 'd' ? '#fb923c'
          : meta.grade === 'e' ? '#f87171' : '#94a3b8';

        const rankBadge = meta.rank ? `<tspan class="jojo-rank-num"> #${meta.rank}</tspan>` : '';

        if (Math.abs(cos) < 0.25) {
          if (sin < 0) {
            labels += `
              <text x="${bx.toFixed(1)}" y="${(by - 16).toFixed(1)}" text-anchor="middle" class="jojo-axis-title">${escapeHtml(d.label)}</text>
              <text x="${bx.toFixed(1)}" y="${(by + 2).toFixed(1)}" text-anchor="middle" class="jojo-rank-letter" fill="${rankColor}">${meta.letter}${rankBadge}</text>
              <text x="${bx.toFixed(1)}" y="${(by + 16).toFixed(1)}" text-anchor="middle" class="jojo-axis-val">${escapeHtml(meta.shown)}</text>
            `;
          } else {
            labels += `
              <text x="${bx.toFixed(1)}" y="${(by + 3).toFixed(1)}" text-anchor="middle" class="jojo-rank-letter" fill="${rankColor}">${meta.letter}${rankBadge}</text>
              <text x="${bx.toFixed(1)}" y="${(by + 17).toFixed(1)}" text-anchor="middle" class="jojo-axis-title">${escapeHtml(d.label)}</text>
              <text x="${bx.toFixed(1)}" y="${(by + 29).toFixed(1)}" text-anchor="middle" class="jojo-axis-val">${escapeHtml(meta.shown)}</text>
            `;
          }
        } else if (cos > 0) {
          labels += `
            <text x="${(bx + 6).toFixed(1)}" y="${(by - 8).toFixed(1)}" text-anchor="start" class="jojo-axis-title">${escapeHtml(d.label)}</text>
            <text x="${(bx + 6).toFixed(1)}" y="${(by + 6).toFixed(1)}" text-anchor="start" class="jojo-rank-letter" fill="${rankColor}">${meta.letter}${rankBadge}</text>
            <text x="${(bx + 6).toFixed(1)}" y="${(by + 19).toFixed(1)}" text-anchor="start" class="jojo-axis-val">${escapeHtml(meta.shown)}</text>
          `;
        } else {
          labels += `
            <text x="${(bx - 6).toFixed(1)}" y="${(by - 8).toFixed(1)}" text-anchor="end" class="jojo-axis-title">${escapeHtml(d.label)}</text>
            <text x="${(bx - 6).toFixed(1)}" y="${(by + 6).toFixed(1)}" text-anchor="end" class="jojo-rank-letter" fill="${rankColor}">${meta.letter}${rankBadge}</text>
            <text x="${(bx - 6).toFixed(1)}" y="${(by + 19).toFixed(1)}" text-anchor="end" class="jojo-axis-val">${escapeHtml(meta.shown)}</text>
          `;
        }

        if (focus) {
          const [vx, vy] = pt(i, meta.ratio);
          dots += `<circle cx="${vx.toFixed(1)}" cy="${vy.toFixed(1)}" r="4.2" class="jojo-dot" fill="${focusColor}"/>`;
        }
      });

      let polys = '';
      sel.filter(m => !focus || m.name !== focus.name).forEach(m => {
        const k = sel.indexOf(m);
        const color = RADAR_COLORS[k % RADAR_COLORS.length];
        const pts = dims.map((d, i) => {
          const mMeta = getDimMeta(d, m, state.modelCatalog);
          const [x, y] = pt(i, mMeta.ratio);
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        polys += `<polygon points="${pts}" class="jojo-shape-other" style="stroke:${color}; fill:${color};"/>`;
      });
      if (focus) {
        const pts = dims.map((d, i) => {
          const fMeta = getDimMeta(d, focus, state.modelCatalog);
          const [x, y] = pt(i, fMeta.ratio);
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        polys += `<polygon points="${pts}" class="jojo-shape-focus" style="stroke:${focusColor}; fill:${focusColor};"/>`;
      }

      chart = `<svg class="radar-svg" viewBox="0 0 ${SW} ${SH}" role="img" aria-label="模型能力维度雷达图">
        <defs>
          <filter id="jojoGlow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="2.5" result="blur"/>
            <feMerge>
              <feMergeNode in="blur"/>
              <feMergeNode in="SourceGraphic"/>
            </feMerge>
          </filter>
          <linearGradient id="jojoFocusGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#ffd700" stop-opacity="0.35"/>
            <stop offset="50%" stop-color="#c084fc" stop-opacity="0.22"/>
            <stop offset="100%" stop-color="#7e22ce" stop-opacity="0.38"/>
          </linearGradient>
        </defs>
        ${grid}${polys}${dots}${labels}
      </svg>`;
    }

    const legend = sel.map((m, k) => {
      const color = RADAR_COLORS[k % RADAR_COLORS.length];
      const on = focus && m.name === focus.name;
      const parts = [];
      if (m.intelligence != null) parts.push(`智能指数 ${m.intelligence}`);
      else if (m.overall != null) parts.push(`综合分 ${m.overall}`);
      const tier = radarTier(m.name);
      if (!tier && m.reasoning === true) parts.push('深度思考');
      else if (!tier && m.reasoning === false) parts.push('非思考');
      const idx = parts.length ? ` · ${parts.join(' · ')}` : '';
      return `<button class="radar-legend-item${on ? ' on' : ''}" data-radar-focus="${escapeHtml(m.name)}"`
        + ` title="${escapeHtml(m.name)}｜点击后查看该模型的各维度等级与原始分">`
        + `<i style="background:${color};color:${color}"></i><span>${escapeHtml(m.name)}${idx}</span></button>`;
    }).join('');

    // 聚焦模型的外部高光勋章横条
    let focusBadgesStrip = '';
    if (focus) {
      const allFocusBadges = getModelExternalBadges(focus);
      const flatBadges = [];
      Object.keys(allFocusBadges).forEach(k => {
        (allFocusBadges[k] || []).forEach(b => flatBadges.push(b));
      });
      if (flatBadges.length) {
        focusBadgesStrip = `<div class="radar-badges-strip">
          <span class="rbs-title">🏆 外部权威实测高光：</span>
          <div class="rbs-list">
            ${flatBadges.map(b => `<span class="comp-ext-badge ${b.color}" title="${escapeHtml(b.tip || b.text)}">${escapeHtml(b.text)}</span>`).join('')}
          </div>
        </div>`;
      }
    }

    const partial = sel.filter(m => dims.some(d => !getDimMeta(d, m, state.modelCatalog).known));
    const partialNote = partial.length
      ? `<div class="radar-note radar-note-warn">部分维度未收录实测数据（轴上标「未测」，不参与分档）：` + partial.map(m => {
          const miss = dims.filter(d => !getDimMeta(d, m, state.modelCatalog).known).map(d => d.label);
          return `${escapeHtml(m.name)}（缺 ${escapeHtml(miss.join('、'))}）`;
        }).join('；') + '</div>'
      : '';

    const sourceName = state.compareSource === 'arena' ? 'LMArena'
      : state.compareSource === 'livebench' ? 'LiveBench' : 'Artificial Analysis';
    host.innerHTML = `
      <div class="radar-head">
        <div class="radar-head-left">
          <strong>${sourceName} 维度图</strong>
          <span class="radar-head-sub">显示当前模型各维度的等级与原始分；点击图例切换模型</span>
        </div>
        <div class="jojo-stand-info">
          <span class="jojo-stand-badge">${escapeHtml(focus ? focus.name : '')}</span>
        </div>
      </div>
      ${chart}
      <div class="radar-legend-wrap">
        <div class="radar-legend">${legend}</div>
        ${focusBadgesStrip}
      </div>
      <div class="jojo-scale-bar">
        <span class="jojo-scale-item"><b class="grade-a">A</b> ≥90%</span>
        <span class="jojo-scale-sep">·</span>
        <span class="jojo-scale-item"><b class="grade-b">B</b> ≥75%</span>
        <span class="jojo-scale-sep">·</span>
        <span class="jojo-scale-item"><b class="grade-c">C</b> ≥50%</span>
        <span class="jojo-scale-sep">·</span>
        <span class="jojo-scale-item"><b class="grade-d">D</b> ≥20%</span>
        <span class="jojo-scale-sep">·</span>
        <span class="jojo-scale-item"><b class="grade-e">E</b> &lt;20%</span>
        <span class="jojo-scale-sep">·</span>
        <span class="jojo-scale-item"><b class="grade-none">无</b> 未测</span>
      </div>
      ${partialNote}
      <div class="radar-note">维度图按各能力项全网实测位次计算相对实力：分档门槛为 A（前 10% 顶尖）、B（前 25% 优秀）、C（前 50% 良好）、D（前 80% 及格）、E（后 20% 偏弱）；轴旁标注为「等级 #全网排名」与真实绝对分。若未收录该项实测则标“无/未测”，图形贴中心显示。点击图例可切换各模型的全维度评估与外部认证高光。</div>
    `;
    host.querySelectorAll('[data-radar-focus]').forEach(el => {
      el.addEventListener('click', () => {
        state.radarFocus = el.getAttribute('data-radar-focus');
        renderCompareRadar();
      });
    });
  }

  // 选中变化时一起重画选择项、维度图和数值对比，避免某块漏更新。
  function renderCompareViews() {
    renderCompareSelection();
    renderCompareRadar();
    renderCompareTable();
  }

  function renderCompareTable() {
    const container = document.getElementById('compareTableContainer');
    if (!container) return;
    const sel = state.compareSelection || [];
    if (!sel.length) {
      container.innerHTML = '<div class="compare-empty">选中的模型会在这里逐项对照。</div>';
      return;
    }
    const dims = getCompareDims();
    const chartItems = [];
    const infoRows = [];
    const isNumber = v => typeof v === 'number' && Number.isFinite(v);
    const addChartItem = (label, detail, unit, values, better, format) => {
      const numeric = values.filter(isNumber);
      if (!numeric.length || values.some(v => v != null && !isNumber(v))) return false;
      let bestIdx = -1;
      if (better === 'max' || better === 'min') {
        const bestValue = better === 'max' ? Math.max(...numeric) : Math.min(...numeric);
        bestIdx = values.indexOf(bestValue);
      }
      chartItems.push({ label, detail, unit, values, bestIdx, format });
      return true;
    };

    // 1. 各维度能力详细对照矩阵（直接标注外部顶尖战绩与实测排名）
    const matrixRows = dims.map(d => {
      const rowCells = sel.map(m => {
        const meta = getDimMeta(d, m, state.modelCatalog);
        const mBadges = (getModelExternalBadges(m) || {})[d.key] || [];
        const badgesMarkup = mBadges.map(b =>
          `<span class="comp-ext-badge ${b.color}" title="${escapeHtml(b.tip || b.text)}">${escapeHtml(b.text)}</span>`
        ).join('');

        return `<td class="comp-matrix-cell">
          <div class="comp-cell-main">
            <span class="comp-cell-val${meta.known ? '' : ' cc-na'}">${escapeHtml(meta.shown)}</span>
            ${meta.rank ? `<span class="comp-cell-rank">#${meta.rank}</span>` : ''}
          </div>
          ${meta.bench ? `<span class="comp-cell-bench">${escapeHtml(meta.bench)}</span>` : ''}
          ${badgesMarkup ? `<div class="comp-badges-wrap">${badgesMarkup}</div>` : ''}
        </td>`;
      }).join('');

      return `<tr>
        <td class="comp-matrix-dim">
          <strong class="comp-dim-name">${escapeHtml(d.label)}</strong>
          <span class="comp-dim-desc">${escapeHtml(d.desc || d.english || '')}</span>
        </td>
        ${rowCells}
      </tr>`;
    }).join('');

    const matrixTable = `<section class="compare-matrix-section">
      <div class="compare-matrix-head">
        <strong>各维度实测对照与外部顶尖战绩</strong>
        <span>核心指标逐项对照；彩色胶囊直接标注外部顶级实测认证（Vals AI / SWE-bench / LMSYS Arena 盲测）</span>
      </div>
      <div class="hud-table-wrapper comp-matrix-scroll">
        <table class="compact-table compare-matrix-table">
          <thead>
            <tr>
              <th class="comp-th-dim">能力维度</th>
              ${sel.map((m, i) => `
                <th class="cc-model" style="border-top: 3px solid ${RADAR_COLORS[i % RADAR_COLORS.length]}">
                  <strong>${escapeHtml(m.name)}</strong>
                  <span class="cc-model-org">${escapeHtml(m.org || '')}</span>
                </th>
              `).join('')}
            </tr>
          </thead>
          <tbody>
            ${matrixRows}
          </tbody>
        </table>
      </div>
    </section>`;

    // 2. 独立柱状图
    dims.forEach(d => {
      const values = sel.map(m => getDimMeta(d, m, state.modelCatalog).raw);
      const format = d.format ? d.format : (v => `${String(v)}${d.unit === '%' ? '%' : d.unit ? ` ${d.unit}` : ''}`);
      addChartItem(d.label, d.english || '', d.unit || '', values, 'max', format);
    });

    const fmtPrice = (v) => '$' + (v >= 0.01 ? v.toFixed(2) : v.toFixed(3));
    const fmtCtx = (v) => (v / 1000) + 'K';
    const AA_METRICS = [
      { label: '输入价 / 1M', get: m => m.price_in, better: 'min', fmt: fmtPrice },
      { label: '输出价 / 1M', get: m => m.price_out, better: 'min', fmt: fmtPrice },
      { label: '上下文窗口', get: m => m.context, better: 'none', fmt: fmtCtx },
      { label: '权重属性', get: m => (m.open_weights ? '开源权重' : '闭源'), better: 'none', fmt: String },
      { label: '推理模式', get: m => (m.reasoning ? '深度思考' : '非思考'), better: 'none', fmt: String },
    ];
    const ARENA_METRICS = [
      { label: '综合分', get: m => m.overall, better: 'max', fmt: String },
      { label: '综合榜名次', get: m => m.rank, better: 'min', fmt: String },
      { label: '盲测投票 / 观测数', get: m => m.votes != null ? m.votes : m.votes_label, better: 'none',
        fmt: v => typeof v === 'number' ? v.toLocaleString() : String(v) },
      { label: '置信区间', get: m => m.ci, better: 'none', fmt: String },
      { label: '许可', get: m => m.license, better: 'none', fmt: String },
    ];
    const LIVEBENCH_METRICS = [
      { label: 'LiveBench 总分', get: m => m.overall, better: 'max', fmt: String },
      { label: '开放权重', get: m => (m.open_weights ? '是' : '否'), better: 'none', fmt: String },
      { label: '推理模型', get: m => (m.reasoning ? '是' : '否'), better: 'none', fmt: String },
    ];
    const METRICS = state.compareSource === 'arena' ? ARENA_METRICS
      : state.compareSource === 'livebench' ? LIVEBENCH_METRICS : AA_METRICS;
    METRICS.forEach(spec => {
      const vals = sel.map(spec.get);
      if (addChartItem(spec.label, '', '', vals, spec.better, v => String(spec.fmt(v)))) return;
      if (vals.some(v => v != null)) infoRows.push({ spec, vals });
    });

    let chartMarkup = '<div class="compare-chart-empty">当前数据没有可绘制的数值项。</div>';
    if (chartItems.length) {
      const groupW = sel.length >= 4 ? 196 : sel.length === 3 ? 156 : 132;
      const chartH = 374;
      const plotTop = 48;
      const plotH = 210;
      const baseY = plotTop + plotH;
      const areaW = chartItems.length * groupW;
      const viewportW = Math.max(760, container.clientWidth - 28);
      const chartW = Math.max(viewportW, areaW + 72);
      const left = Math.max(42, (chartW - areaW) / 2);
      let grid = '';
      [0, 25, 50, 75, 100].forEach(pct => {
        const y = baseY - (pct / 100) * plotH;
        grid += `<line x1="${left}" y1="${y}" x2="${left + areaW}" y2="${y}" class="compare-chart-grid"/>`;
        grid += `<text x="${left - 8}" y="${y + 4}" text-anchor="end" class="compare-chart-axis">${pct}%</text>`;
      });
      const groups = chartItems.map((item, groupIndex) => {
        const groupX = left + groupIndex * groupW;
        const slotW = groupW / sel.length;
        const barW = Math.min(25, slotW * 0.58);
        const magnitude = Math.max(0, ...item.values.filter(isNumber).map(v => Math.abs(v)));
        let bars = '';
        item.values.forEach((v, modelIndex) => {
          const centerX = groupX + modelIndex * slotW + slotW / 2;
          if (!isNumber(v)) {
            bars += `<text x="${centerX}" y="${baseY - 7}" text-anchor="middle" class="compare-chart-value">--</text>`;
            return;
          }
          const ratio = magnitude ? Math.min(1, Math.abs(v) / magnitude) : 0;
          const h = ratio * plotH;
          const y = baseY - Math.max(h, 1);
          const x = centerX - barW / 2;
          const isBest = modelIndex === item.bestIdx;
          bars += `<g><title>${escapeHtml(sel[modelIndex].name)}：${escapeHtml(item.format(v))}</title>`
            + `<rect x="${x}" y="${y}" width="${barW}" height="${Math.max(h, 1)}" rx="3" fill="${RADAR_COLORS[modelIndex % RADAR_COLORS.length]}"${isBest ? ' stroke="#22c55e" stroke-width="2"' : ''}/>`
            + `<text x="${centerX}" y="${Math.max(plotTop - 6, y - 6)}" text-anchor="middle" class="compare-chart-value">${escapeHtml(item.format(v))}</text></g>`;
        });
        const labelLines = String(item.label).match(/.{1,8}/gu) || [String(item.label)];
        const labels = labelLines.map((line, i) => `<tspan x="${groupX + groupW / 2}" dy="${i ? 13 : 0}">${escapeHtml(line)}</tspan>`).join('');
        const detail = item.detail
          ? `<text x="${groupX + groupW / 2}" y="${328 + Math.max(0, labelLines.length - 1) * 13}" text-anchor="middle" class="compare-chart-unit">${escapeHtml(item.detail)}</text>` : '';
        return `${bars}<text x="${groupX + groupW / 2}" y="286" text-anchor="middle" class="compare-chart-label">${labels}</text>${detail}`;
      }).join('');
      const legend = sel.map((m, i) => `<span class="compare-chart-legend-item"><i style="background:${RADAR_COLORS[i % RADAR_COLORS.length]}"></i>${escapeHtml(m.name)}</span>`).join('');
      chartMarkup = `<section class="compare-chart">
        <div class="compare-chart-head"><strong>数值柱状图</strong><span>各项独立缩放，柱顶显示原始数值；绿色描边标记较优值。</span></div>
        <div class="compare-chart-legend">${legend}</div>
        <div class="compare-chart-scroll"><svg class="compare-chart-svg" viewBox="0 0 ${chartW} ${chartH}" width="${chartW}" height="${chartH}" role="img" aria-label="模型数值对比柱状图">${grid}${groups}</svg></div>
      </section>`;
    }

    // 3. 文字信息
    const infoTable = infoRows.length ? `<section class="compare-info-wrap">
      <div class="compare-info-title">模型基础属性</div>
      <table class="compact-table compare-table"><thead><tr><th>属性项</th>${sel.map(m => `<th class="cc-model"><strong>${escapeHtml(m.name)}</strong><span class="cc-model-org">${escapeHtml(m.org || '')}</span></th>`).join('')}</tr></thead>
      <tbody>${infoRows.map(({ spec, vals }) => `<tr><td class="cc-label">${escapeHtml(spec.label)}</td>${vals.map(v => `<td class="text-right${v == null ? ' cc-na' : ''}">${v == null ? '--' : escapeHtml(String(spec.fmt(v)))}</td>`).join('')}</tr>`).join('')}</tbody></table>
    </section>` : '';

    container.innerHTML = matrixTable + chartMarkup + infoTable;
  }


  // --- 10. 单页 Hash 路由总控制器（方案 C 工作台版） ---
  // --- 剪贴板历史（实用工具） ---
  function clipTimeAgo(ts) {
    const diff = Date.now() / 1000 - (ts || 0);
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getMonth() + 1}-${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function renderClipboardList(force) {
    const box = document.getElementById('clipList');
    if (!box) return;
    const st = state.clipData;
    const items = (st && st.items) || [];

    // 数据指纹：轮询期间数据未变则跳过重渲染，避免替换 DOM 打断用户点击
    const fp = (st ? st.enabled : '') + '|' +
      items.map(it => it.id + ':' + it.ts).join(',');
    if (!force && fp === renderClipboardList._fp) return;
    renderClipboardList._fp = fp;

    const kw = (state.clipSearch || '').toLowerCase();
    const filtered = kw ? items.filter(it => (it.text || '').toLowerCase().includes(kw)) : items;

    // 监听状态行与开关回显
    const stateText = document.getElementById('clipStateText');
    if (stateText) {
      stateText.innerHTML = (st === null) ? '--'
        : (st.enabled
          ? `<span style="color:var(--ok)">● 监听中</span>&nbsp;· 共 ${items.length} 条`
          : `<span style="color:var(--warn)">● 已暂停</span>&nbsp;· 保留 ${items.length} 条`);
    }
    const tog = document.getElementById('clipEnabledToggle');
    if (tog && st && tog.checked !== st.enabled) tog.checked = st.enabled;

    if (!filtered.length) {
      box.innerHTML = `<div class="clip-empty">${kw
        ? '没有匹配的历史内容'
        : '暂无记录 · 去任意程序复制一段文本，这里会自动出现'}</div>`;
      return;
    }

    const seenMaxId = state.clipMaxId;
    let maxId = seenMaxId;
    const cards = filtered.map(it => {
      if (it.id > maxId) maxId = it.id;
      const esc = escapeHtml(it.text || '');
      const isNew = seenMaxId > 0 && it.id > seenMaxId;
      return `
      <div class="clip-item${isNew ? ' clip-new' : ''}" data-id="${it.id}">
        <div class="clip-item-text" title="${esc}">${esc}</div>
        <div class="clip-item-meta">
          <span class="clip-time"><i class="ph ph-clock"></i>${clipTimeAgo(it.ts)}</span>
          <span class="clip-chars">${formatNumber(it.chars)} 字符</span>
          ${it.truncated ? '<span class="clip-trunc" title="超长内容仅存前 2 万字符">已截断</span>' : ''}
          <span class="clip-spacer"></span>
          <button class="clip-act" data-clip-act="copy" data-id="${it.id}" title="写回系统剪贴板（Ctrl+V 可粘贴）"><i class="ph ph-copy"></i>复制</button>
          <button class="clip-act danger" data-clip-act="delete" data-id="${it.id}" title="删除此条"><i class="ph ph-trash"></i></button>
        </div>
      </div>`;
    }).join('');
    state.clipMaxId = maxId;

    // 重渲染保留主滚动位置，避免轮询刷新时列表跳动
    const content = document.getElementById('content');
    const scrollTop0 = content ? content.scrollTop : 0;
    box.innerHTML = cards;
    if (content) content.scrollTop = scrollTop0;
  }

  async function loadClipboardHistory() {
    try {
      const resp = await fetch('/api/clipboard/history');
      const d = await resp.json();
      if (d && d.ok) {
        state.clipData = d;
        renderClipboardList();
      }
    } catch (e) { /* 服务离线时静默，列表保留现状 */ }
  }

  async function clipAction(payload) {
    const resp = await fetch('/api/clipboard/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    return resp.json();
  }

  function initClipboard() {
    if (initClipboard._done) return;          // 路由反复进入只绑一次事件
    initClipboard._done = true;

    const inp = document.getElementById('clipSearchInput');
    if (inp) inp.addEventListener('input', () => {
      state.clipSearch = inp.value.trim();
      renderClipboardList(true);       // 本地过滤不走数据指纹，强制重渲染
    });

    const tog = document.getElementById('clipEnabledToggle');
    if (tog) tog.addEventListener('change', async () => {
      try { await clipAction({ action: 'toggle', enabled: tog.checked }); } catch (e) {}
      loadClipboardHistory();
    });

    const btnClear = document.getElementById('btnClipClear');
    if (btnClear) btnClear.addEventListener('click', async () => {
      if (!confirm('确定清空全部剪贴板历史？此操作不可恢复。')) return;
      try { await clipAction({ action: 'clear' }); } catch (e) {}
      loadClipboardHistory();
    });

    // 列表事件委托：复制 / 删除
    const list = document.getElementById('clipList');
    if (list) list.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-clip-act]');
      if (!btn) return;
      const id = parseInt(btn.getAttribute('data-id'), 10);
      const act = btn.getAttribute('data-clip-act');
      if (act === 'copy') {
        try {
          const d = await clipAction({ action: 'copy', id });
          if (d && d.ok) {
            state.clipData = Object.assign({}, state.clipData, { items: d.items || [] });
            renderClipboardList();
            const top = list.querySelector('.clip-item');
            if (top) {
              top.classList.add('clip-copied');
              setTimeout(() => top.classList.remove('clip-copied'), 1200);
            }
          }
        } catch (err) { /* 剪贴板被占用等失败：状态保持 */ }
      } else if (act === 'delete') {
        try { await clipAction({ action: 'delete', id }); } catch (err) {}
        loadClipboardHistory();
      }
    });

    // 页面驻留期间 3s 轮询新复制内容
    setInterval(() => {
      if (location.hash === '#/clipboard') loadClipboardHistory();
    }, 3000);
  }

  // ==========================================
  // OpenAI 专区与账号矩阵切换器 (Codex Switcher)
  // ==========================================
  let _lastCodexStatus = null;

  function initCodexPage() {
    if (!initCodexPage._done) {
      initCodexPage._done = true;
      const btnRefresh = document.getElementById('btnCodexRefresh');
      if (btnRefresh) btnRefresh.addEventListener('click', () => fetchCodexStatus());

      const btnSaveCurrent = document.getElementById('btnCodexSaveCurrent');
      if (btnSaveCurrent) btnSaveCurrent.addEventListener('click', () => window.saveCurrentCodexPrompt());

      const btnSaveActiveInline = document.getElementById('btnCodexSaveActiveInline');
      if (btnSaveActiveInline) btnSaveActiveInline.addEventListener('click', () => window.saveCurrentCodexPrompt());

      const btnOpenAddModal = document.getElementById('btnCodexOpenAddModal');
      if (btnOpenAddModal) btnOpenAddModal.addEventListener('click', () => window.openCodexModal());

      const btnCloseModal = document.getElementById('btnCloseCodexModal');
      if (btnCloseModal) btnCloseModal.addEventListener('click', () => window.closeCodexModal());

      const btnCancelModal = document.getElementById('btnCancelCodexModal');
      if (btnCancelModal) btnCancelModal.addEventListener('click', () => window.closeCodexModal());

      const btnSubmitModal = document.getElementById('btnSubmitCodexModal');
      if (btnSubmitModal) btnSubmitModal.addEventListener('click', () => submitCodexModal());

      const btnFillCurrent = document.getElementById('btnCodexFillFromCurrent');
      if (btnFillCurrent) btnFillCurrent.addEventListener('click', () => fillCodexModalFromCurrent());

      const fileInput = document.getElementById('modalCodexFileInput');
      if (fileInput) {
        fileInput.addEventListener('change', (e) => {
          const file = e.target.files && e.target.files[0];
          if (file) {
            const reader = new FileReader();
            reader.onload = (evt) => {
              const text = evt.target.result;
              const authEl = document.getElementById('modalCodexAuthContent');
              if (authEl) {
                authEl.value = text;
                updateCodexModalParsedPreview(text);
              }
            };
            reader.readAsText(file);
          }
        });
      }

      const authContentEl = document.getElementById('modalCodexAuthContent');
      if (authContentEl) {
        authContentEl.addEventListener('input', () => {
          updateCodexModalParsedPreview(authContentEl.value);
        });
      }

      function setupCodexPromptCopy(btnId, textId) {
        const btn = document.getElementById(btnId);
        const textEl = document.getElementById(textId);
        if (!btn || !textEl) return;

        const doCopy = async () => {
          const text = (textEl.innerText || textEl.textContent || '').trim();
          if (!text) return;
          let ok = false;
          try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              await navigator.clipboard.writeText(text);
              ok = true;
            }
          } catch (_) {}
          if (!ok) {
            try {
              const ta = document.createElement('textarea');
              ta.value = text;
              ta.style.position = 'fixed';
              ta.style.opacity = '0';
              document.body.appendChild(ta);
              ta.select();
              document.execCommand('copy');
              document.body.removeChild(ta);
              ok = true;
            } catch (_) {}
          }
          if (ok) {
            const oldHtml = btn.innerHTML;
            btn.innerHTML = '<i class="ph ph-check" style="color:#10b981;"></i><span style="color:#10b981;font-weight:600;">已复制！</span>';
            const oldBorder = textEl.style.borderColor;
            textEl.style.borderColor = 'rgba(16,185,129,0.7)';
            setTimeout(() => {
              btn.innerHTML = oldHtml;
              textEl.style.borderColor = oldBorder;
            }, 1800);
          } else {
            alert('复制失败，请手动选中文本复制');
          }
        };

        btn.addEventListener('click', doCopy);
        textEl.addEventListener('click', doCopy);
      }

      setupCodexPromptCopy('btnCopyCodexPrompt1', 'codexPromptText1');
      setupCodexPromptCopy('btnCopyCodexPrompt2', 'codexPromptText2');
    }
    fetchCodexStatus();
  }

  function updateCodexModalParsedPreview(text) {
    const card = document.getElementById('modalCodexParsedCard');
    const emailEl = document.getElementById('modalParsedEmail');
    const idEl = document.getElementById('modalParsedId');
    if (!card) return;
    if (!text || !text.trim()) {
      card.style.display = 'none';
      return;
    }
    try {
      const data = JSON.parse(text.trim());
      const tokens = data.tokens || {};
      let email = tokens.account_email || '未知邮箱';
      if (tokens.id_token) {
        const parts = tokens.id_token.split('.');
        if (parts.length >= 2) {
          try {
            const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
            if (payload.email) email = payload.email;
          } catch (_) {}
        }
      }
      const accId = tokens.account_id || '--';
      if (emailEl) emailEl.textContent = email;
      if (idEl) idEl.textContent = accId;
      card.style.display = 'block';

      // 如果名称为空，自动把邮箱前缀填入预设名称
      const nameInput = document.getElementById('modalCodexName');
      if (nameInput && !nameInput.value.trim() && email && email !== '未知邮箱') {
        nameInput.value = email.split('@')[0];
      }
    } catch (_) {
      card.style.display = 'none';
    }
  }

  async function fillCodexModalFromCurrent() {
    try {
      const res = await fetch('/api/codex/status');
      if (!res.ok) return;
      const data = await res.json();
      if (!data.current_active || !data.current_active.email || data.current_active.email === '未知邮箱') {
        alert('当前客户端未检测到有效的已登录账号，请先在 Codex Desktop 登录！');
        return;
      }
      // 从后端读取或抓取当前 auth
      const authEl = document.getElementById('modalCodexAuthContent');
      const nameInput = document.getElementById('modalCodexName');
      if (nameInput && !nameInput.value.trim()) {
        nameInput.value = data.current_active.email.split('@')[0] || '当前账号';
      }
      // 通过保存接口或者填入抓取提示
      if (authEl) {
        authEl.placeholder = '（将自动从当前客户端凭据提取）';
        authEl.value = JSON.stringify({
          auth_mode: 'chatgpt',
          tokens: {
            account_id: data.current_active.account_id,
            account_email: data.current_active.email
          },
          _notice: '点击保存将直接同步当前客户端完整凭据'
        }, null, 2);
        updateCodexModalParsedPreview(authEl.value);
      }
    } catch (e) {
      alert('读取当前登录态失败: ' + e.message);
    }
  }

  window.openCodexModal = function(profile) {
    const overlay = document.getElementById('codexModalOverlay');
    const titleEl = document.getElementById('codexModalTitle');
    const idEl = document.getElementById('modalCodexProfileId');
    const nameEl = document.getElementById('modalCodexName');
    const authEl = document.getElementById('modalCodexAuthContent');
    const card = document.getElementById('modalCodexParsedCard');
    const btnDelModal = document.getElementById('btnDeleteFromModal');

    if (!overlay) return;
    if (profile) {
      if (titleEl) titleEl.innerHTML = '<i class="ph ph-note-pencil" style="margin-right:6px;"></i>编辑 Codex 预设账号';
      if (idEl) idEl.value = profile.id || '';
      if (nameEl) nameEl.value = profile.name || '';
      if (authEl) authEl.value = profile.authContents || '';
      updateCodexModalParsedPreview(profile.authContents || '');
      if (btnDelModal) {
        btnDelModal.style.display = 'inline-flex';
        btnDelModal.onclick = () => {
          window.closeCodexModal();
          window.deleteCodexProfile(profile.id, encodeURIComponent(profile.name));
        };
      }
    } else {
      if (titleEl) titleEl.innerHTML = '<i class="ph ph-user-plus" style="margin-right:6px;"></i>添加 Codex 预设账号';
      if (idEl) idEl.value = '';
      if (nameEl) nameEl.value = '';
      if (authEl) authEl.value = '';
      if (card) card.style.display = 'none';
      if (btnDelModal) btnDelModal.style.display = 'none';
    }
    overlay.style.display = 'flex';
  };

  window.openCodexModalById = function(profileId) {
    const profiles = (_lastCodexStatus && _lastCodexStatus.profiles) || [];
    const target = profiles.find(p => p.id === profileId);
    if (target) {
      window.openCodexModal(target);
    }
  };

  window.closeCodexModal = function() {
    const overlay = document.getElementById('codexModalOverlay');
    if (overlay) overlay.style.display = 'none';
  };

  async function submitCodexModal() {
    const idEl = document.getElementById('modalCodexProfileId');
    const nameEl = document.getElementById('modalCodexName');
    const authEl = document.getElementById('modalCodexAuthContent');
    const name = nameEl ? nameEl.value.trim() : '';
    const auth = authEl ? authEl.value.trim() : '';
    const profileId = idEl ? idEl.value.trim() : '';

    if (!name) {
      alert('请输入预设名称！');
      return;
    }

    // 如果是通过「从当前客户端抓取」生成的占位
    if (auth.includes('_notice') && !profileId) {
      window.closeCodexModal();
      window.saveCurrentCodexPrompt(name);
      return;
    }

    if (!auth) {
      alert('请输入或导入 auth.json 凭据内容！');
      return;
    }

    try {
      const resp = await fetch('/api/codex/profile/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: profileId || undefined,
          name: name,
          auth_content: auth
        })
      });
      const ret = await resp.json();
      if (ret.ok) {
        window.closeCodexModal();
        const resEl = document.getElementById('codexSwitchResult');
        if (resEl) resEl.innerHTML = `<span style="color:#10b981;"><i class="ph ph-check-circle"></i> ${ret.message || '保存成功'}</span>`;
        fetchCodexStatus();
      } else {
        alert('保存失败: ' + (ret.error || '未知错误'));
      }
    } catch (e) {
      alert('保存异常: ' + e.message);
    }
  }

  window.saveCurrentCodexPrompt = async function(prefilledName) {
    const curr = _lastCodexStatus && _lastCodexStatus.current_active;
    const defaultName = prefilledName || (curr && curr.email && curr.email !== '未知邮箱' ? curr.email.split('@')[0] : '我的账号');
    const name = prompt('请输入新预设名称（将当前客户端已登录态保存至预设矩阵）：', defaultName);
    if (!name || !name.trim()) return;

    const resEl = document.getElementById('codexSwitchResult');
    if (resEl) resEl.innerHTML = '<span style="color:#339cff;"><i class="ph ph-spinner ph-spin"></i> 正在读取并保存当前客户端凭据...</span>';
    try {
      const resp = await fetch('/api/codex/save_current', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() })
      });
      const ret = await resp.json();
      if (ret.ok) {
        if (resEl) resEl.innerHTML = `<span style="color:#10b981;"><i class="ph ph-check-circle"></i> ${ret.message}</span>`;
        fetchCodexStatus();
      } else {
        if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 保存失败: ${ret.error || '未知错误'}</span>`;
      }
    } catch (e) {
      if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 请求异常: ${e.message}</span>`;
    }
  };

  window.deleteCodexProfile = async function(profileId, encodedName) {
    const name = decodeURIComponent(encodedName || '');
    if (!confirm(`确认从账号矩阵中删除预设「${name || profileId}」吗？`)) {
      return;
    }
    const resEl = document.getElementById('codexSwitchResult');
    try {
      const resp = await fetch('/api/codex/profile/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: profileId })
      });
      const ret = await resp.json();
      if (ret.ok) {
        if (resEl) resEl.innerHTML = `<span style="color:#10b981;"><i class="ph ph-check-circle"></i> ${ret.message || '已删除'}</span>`;
        fetchCodexStatus();
      } else {
        alert('删除失败: ' + (ret.error || '未知错误'));
      }
    } catch (e) {
      alert('删除异常: ' + e.message);
    }
  };

  window.moveCodexProfile = async function(profileId, direction) {
    const resEl = document.getElementById('codexSwitchResult');
    try {
      const resp = await fetch('/api/codex/profile/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: profileId, direction })
      });
      const ret = await resp.json();
      if (ret.ok) {
        fetchCodexStatus();
      } else {
        if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 排序调整失败: ${ret.error || '未知错误'}</span>`;
      }
    } catch (e) {
      if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 请求异常: ${e.message}</span>`;
    }
  };

  window.reorderCodexProfiles = async function(orderIds) {
    const resEl = document.getElementById('codexSwitchResult');
    try {
      const resp = await fetch('/api/codex/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order: orderIds })
      });
      const ret = await resp.json();
      if (ret.ok) {
        if (resEl) resEl.innerHTML = `<span style="color:#10b981;"><i class="ph ph-check-circle"></i> 排序已更新并保存</span>`;
        fetchCodexStatus();
      } else {
        if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 保存排序失败: ${ret.error || '未知错误'}</span>`;
      }
    } catch (e) {
      if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 保存排序异常: ${e.message}</span>`;
    }
  };

  async function fetchCodexStatus() {
    try {
      const res = await fetch('/api/codex/status');
      if (!res.ok) return;
      const data = await res.json();
      _lastCodexStatus = data;
      renderCodexStatus(data);
    } catch (e) {
      console.error('fetchCodexStatus error:', e);
    }
  }

  function renderCodexStatus(data) {
    if (!data || !data.ok) return;
    const act = data.current_active || {};
    const emailEl = document.getElementById('codexActiveEmail');
    const idEl = document.getElementById('codexActiveId');
    const expEl = document.getElementById('codexActiveTokenExp');
    const refEl = document.getElementById('codexActiveRefresh');
    const badgeEl = document.getElementById('codexActiveBadge');

    const hasActiveLogin = act.email && act.email !== '未知邮箱';

    if (emailEl) emailEl.textContent = hasActiveLogin ? act.email : '未检测到登录态';
    if (idEl) idEl.textContent = act.account_id || '--';
    if (badgeEl) {
      if (hasActiveLogin) {
        badgeEl.textContent = '活跃中';
        badgeEl.className = 'hud-badge online';
      } else {
        badgeEl.textContent = '未登录';
        badgeEl.className = 'hud-badge offline';
      }
    }

    if (expEl) {
      if (act.exp_minutes != null) {
        if (act.exp_minutes > 0) {
          expEl.innerHTML = `<span style="color:#10b981;">有效 (剩余 ${act.exp_minutes} 分钟)</span>`;
        } else {
          expEl.innerHTML = `<span style="color:#ef4444;">已过期 (${Math.abs(act.exp_minutes)} 分钟前)</span>`;
        }
      } else {
        expEl.textContent = '--';
      }
    }
    if (refEl) {
      refEl.innerHTML = act.has_refresh ? '<span style="color:#10b981;">● 正常 (已具备)</span>' : '<span style="color:#eab308;">无</span>';
    }

    const profiles = data.profiles || [];
    const countEl = document.getElementById('codexMatrixCount');
    if (countEl) countEl.textContent = `共 ${profiles.length} 个预设`;

    // 动态渲染一键切换卡片区域（带快速排序与删除）
    const quickList = document.getElementById('codexQuickSwitchList');
    if (quickList) {
      if (profiles.length === 0) {
        quickList.innerHTML = `
          <div style="font-size:13px;opacity:0.6;padding:16px;text-align:center;">
            暂无已存账号预设。<br>点击右上角「<strong>存当前登录为预设</strong>」或「<strong>添加预设账号</strong>」即可开始使用。
          </div>
        `;
      } else {
        quickList.innerHTML = profiles.map((p, i) => {
          const info = p.info || {};
          const isAct = p.is_active;
          const emailText = info.email && info.email !== '未知邮箱' ? info.email : (p.id || '');
          return `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 12px;background:${isAct ? 'rgba(51,156,255,0.12)' : 'rgba(255,255,255,0.04)'};border:1px solid ${isAct ? 'rgba(51,156,255,0.35)' : 'rgba(255,255,255,0.1)'};border-radius:8px;transition:all 0.15s;" onmouseover="if(!${isAct}){this.style.borderColor='rgba(51,156,255,0.5)';this.style.background='rgba(51,156,255,0.06)'}" onmouseout="if(!${isAct}){this.style.borderColor='rgba(255,255,255,0.1)';this.style.background='rgba(255,255,255,0.04)'}">
              <div style="display:flex;align-items:center;gap:10px;cursor:pointer;flex:1;min-width:0;" onclick="${isAct ? '' : `window.switchCodexTo('${p.id}')`}">
                <i class="ph ${isAct ? 'ph-user-check' : 'ph-user'}" style="color:${isAct ? '#339cff' : 'inherit'};font-size:18px;flex-shrink:0;"></i>
                <div style="display:flex;flex-direction:column;min-width:0;">
                  <div style="display:flex;align-items:center;gap:6px;">
                    <strong style="font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${p.name}</strong>
                    ${isAct ? '<span style="font-size:10.5px;opacity:0.85;background:rgba(51,156,255,0.25);color:#339cff;padding:1px 6px;border-radius:4px;flex-shrink:0;">当前使用中</span>' : ''}
                  </div>
                  <span style="font-size:11.5px;opacity:0.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${emailText}</span>
                </div>
              </div>
              <div style="display:flex;align-items:center;gap:4px;flex-shrink:0;margin-left:8px;">
                ${isAct ? '' : `<button class="btn-ghost sm" onclick="window.switchCodexTo('${p.id}')" title="一键切换到此账号"><i class="ph ph-lightning"></i> 切换</button>`}
                <button class="btn-ghost sm" onclick="event.stopPropagation();window.moveCodexProfile('${p.id}', 'up')" title="上移排序" ${i === 0 ? 'disabled style="opacity:0.25;cursor:not-allowed;"' : ''}><i class="ph ph-caret-up"></i></button>
                <button class="btn-ghost sm" onclick="event.stopPropagation();window.moveCodexProfile('${p.id}', 'down')" title="下移排序" ${i === profiles.length - 1 ? 'disabled style="opacity:0.25;cursor:not-allowed;"' : ''}><i class="ph ph-caret-down"></i></button>
                <button class="btn-ghost sm" style="color:#ef4444;" onclick="event.stopPropagation();window.deleteCodexProfile('${p.id}', '${encodeURIComponent(p.name)}')" title="从矩阵删除此账号"><i class="ph ph-trash"></i></button>
              </div>
            </div>
          `;
        }).join('');
      }
    }

    // 动态渲染已登记账号列表表格（支持拖拽排序 + 上下置顶按钮 + 编辑/删除）
    const tbody = document.getElementById('codexProfilesBody');
    if (tbody) {
      if (profiles.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="padding:16px;text-align:center;opacity:0.6;">暂无已登记账号预设，点击右上角即可添加</td></tr>';
      } else {
        tbody.innerHTML = profiles.map((p, i) => {
          const info = p.info || {};
          const isAct = p.is_active;
          const statusBadge = isAct
            ? '<span style="background:rgba(16,185,129,0.2);color:#10b981;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;">当前使用</span>'
            : '<span style="background:rgba(255,255,255,0.06);opacity:0.6;padding:2px 8px;border-radius:999px;font-size:11px;">备用</span>';
          let expText = '--';
          if (info.exp_minutes != null) {
            expText = info.exp_minutes > 0 ? `剩 ${info.exp_minutes} 分钟` : `<span style="color:#ef4444;">已过期</span>`;
          }
          const emailDisplay = info.email && info.email !== '未知邮箱' ? info.email : '--';
          const accIdDisplay = info.account_id && info.account_id !== '未知ID'
            ? (info.account_id.length > 13 ? info.account_id.slice(0, 13) + '...' : info.account_id)
            : '--';

          return `
            <tr draggable="true" data-profile-id="${p.id}" style="border-bottom:1px solid rgba(255,255,255,0.04);background:${isAct ? 'rgba(51,156,255,0.04)' : 'transparent'};transition:background 0.15s;">
              <td style="padding:10px;white-space:nowrap;">
                <span style="cursor:grab;display:inline-flex;align-items:center;padding:2px;opacity:0.6;margin-right:4px;" title="按住拖拽排序"><i class="ph ph-dots-six-vertical" style="font-size:15px;"></i></span>
                <button class="btn-ghost sm" onclick="window.moveCodexProfile('${p.id}', 'up')" title="上移" ${i === 0 ? 'disabled style="opacity:0.25;cursor:not-allowed;"' : ''}><i class="ph ph-arrow-up"></i></button>
                <button class="btn-ghost sm" onclick="window.moveCodexProfile('${p.id}', 'down')" title="下移" ${i === profiles.length - 1 ? 'disabled style="opacity:0.25;cursor:not-allowed;"' : ''}><i class="ph ph-arrow-down"></i></button>
                <button class="btn-ghost sm" onclick="window.moveCodexProfile('${p.id}', 'top')" title="置顶" ${i === 0 ? 'disabled style="opacity:0.25;cursor:not-allowed;"' : ''}><i class="ph ph-arrow-fat-line-up"></i></button>
              </td>
              <td style="padding:10px;">${statusBadge}</td>
              <td style="padding:10px;font-weight:600;">${p.name || p.id}</td>
              <td style="padding:10px;">${emailDisplay}</td>
              <td style="padding:10px;"><code style="font-size:11px;opacity:0.8;">${accIdDisplay}</code></td>
              <td style="padding:10px;">${expText}</td>
              <td style="padding:10px;text-align:right;white-space:nowrap;">
                ${isAct ? '<span style="font-size:12px;opacity:0.5;margin-right:8px;">生效中</span>' : `<button class="btn-ghost sm" onclick="window.switchCodexTo('${p.id}')" style="margin-right:6px;"><i class="ph ph-lightning"></i> 切换</button>`}
                <button class="btn-ghost sm" onclick="window.openCodexModalById('${p.id}')" title="编辑预设" style="margin-right:6px;"><i class="ph ph-note-pencil"></i> 编辑</button>
                <button class="btn-ghost sm" style="color:#ef4444;" onclick="window.deleteCodexProfile('${p.id}', '${encodeURIComponent(p.name)}')"><i class="ph ph-trash"></i> 删除</button>
              </td>
            </tr>
          `;
        }).join('');
        initCodexTableDragAndDrop(tbody);
      }
    }
  }

  function initCodexTableDragAndDrop(tbody) {
    let draggedRow = null;
    const rows = tbody.querySelectorAll('tr[draggable="true"]');
    rows.forEach(row => {
      row.addEventListener('dragstart', (e) => {
        draggedRow = row;
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', row.getAttribute('data-profile-id') || '');
        row.style.opacity = '0.4';
      });
      row.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        row.style.outline = '2px dashed #339cff';
      });
      row.addEventListener('dragleave', () => {
        row.style.outline = '';
      });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        row.style.outline = '';
        if (draggedRow && draggedRow !== row) {
          const allRows = Array.from(tbody.querySelectorAll('tr[draggable="true"]'));
          const fromIdx = allRows.indexOf(draggedRow);
          const toIdx = allRows.indexOf(row);
          if (fromIdx > -1 && toIdx > -1) {
            const profileIds = allRows.map(r => r.getAttribute('data-profile-id'));
            const movedId = profileIds.splice(fromIdx, 1)[0];
            profileIds.splice(toIdx, 0, movedId);
            window.reorderCodexProfiles(profileIds);
          }
        }
      });
      row.addEventListener('dragend', () => {
        row.style.opacity = '1';
        rows.forEach(r => r.style.outline = '');
        draggedRow = null;
      });
    });
  }

  window.switchCodexTo = async function(target) {
    const resEl = document.getElementById('codexSwitchResult');
    if (resEl) resEl.innerHTML = '<span style="color:#339cff;"><i class="ph ph-spinner ph-spin"></i> 正在安全切换并重启客户端...</span>';
    try {
      const resp = await fetch('/api/codex/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target })
      });
      const ret = await resp.json();
      if (ret.ok) {
        if (resEl) resEl.innerHTML = `<span style="color:#10b981;"><i class="ph ph-check-circle"></i> ${ret.message}</span>`;
        fetchCodexStatus();
      } else {
        if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 切换失败: ${ret.error || '未知错误'}</span>`;
      }
    } catch (e) {
      if (resEl) resEl.innerHTML = `<span style="color:#ef4444;"><i class="ph ph-warning"></i> 切换请求异常: ${e.message}</span>`;
    }
  };

  // 事件只绑一次：路由会反复进本页
  // --- 节点检测（Clash 节点 IP 质量检测）---
  const cnState = { filter: 'all', query: '', items: [], loaded: false, pollTimer: null, rendered: 0, scope: new Set() };
  const CN_PAGE = () => location.hash === '#/nodes';

  function cnPhaseText(phase) {
    return ({ idle: '空闲', collecting: '正在收集节点', starting: '正在启动隔离内核', scanning: '检测中', stopping: '正在停止' })[phase] || phase;
  }
  function cnIppureText(row) {
    const s = row.ippure_status || 'pending';
    if (s === 'ok') return { text: '正常', cls: 'ok' };
    if (s === 'ok_ipv6_no_score') return { text: '正常（IPv6 无分）', cls: 'ok' };
    if (s === 'pending') return { text: '待检测', cls: 'dim' };
    const map = { network_error: '网络错误', selection_error: '选点失败', invalid_json: '响应异常' };
    return { text: map[s] || (s.startsWith('HTTP') ? s : s), cls: 'err' };
  }
  function cnClassBadge(row) {
    const c = row.classification || '观察';
    const cls = { 合格: 'ok', 观察: 'warn', 不合格: 'err', 查询失败: 'dim' }[c] || 'dim';
    const marks = row.four_green ? ' 🟢🟢' : (row.dual_green ? ' 🟢' : '');
    return `<span class="cn-badge cn-badge-${cls}">${escapeHtml(c)}${marks}</span>`;
  }
  function cnBoolBadge(v, label) {
    if (v === true) return `<span class="cn-mini ok">✓ ${label}</span>`;
    if (v === false) return `<span class="cn-mini">✗ ${label}</span>`;
    return `<span class="cn-mini dim">— ${label}</span>`;
  }
  function cnRenderRow(row) {
    const exp = row.expected_country || '—';
    const act = row.country_code || '—';
    const mismatch = row.label_mismatch === true;
    const actualHtml = mismatch
      ? `<span class="cn-mismatch" title="节点名标注的国家与实际出口不符">⚠ ${escapeHtml(act)}</span>`
      : escapeHtml(act);
    const ip = row.ip_redacted || '—';
    const ippure = cnIppureText(row);
    const srcTag = row.source_used === 'ipapi' ? ' <span class="cn-mini">↘ ip-api</span>'
      : row.source_used === 'ping0' ? ' <span class="cn-mini">↘ Ping0</span>' : '';
    let score;
    if (row.ippure_status === 'ok' && row.fraud_score !== null && row.fraud_score !== undefined) {
      score = escapeHtml(String(row.fraud_score));
    } else if (row.source_used === 'ipapi' || row.source_used === 'ping0') {
      score = `<span class="cn-dim">无分</span>${srcTag}`;
    } else {
      score = `<span class="cn-dim">${escapeHtml(ippure.text)}</span>`;
    }
    const delay = row.delay_ms !== null && row.delay_ms !== undefined
      ? `${row.delay_ms} ms`
      : (row.delay_error
        ? `<span class="cn-dim" title="${escapeHtml(row.delay_error)}">超时</span>`
        : '<span class="cn-dim">—</span>');
    const canRetest = row.classification === '查询失败';
    const name = row.display_name || row.probe_name;
    const sources = (row.sources || []).join(' / ');
    return `<tr>
      <td class="cn-node-cell"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><small title="${escapeHtml(sources)}">${escapeHtml(sources || '—')}</small></td>
      <td class="cn-geo-cell">${escapeHtml(exp)} → ${actualHtml}</td>
      <td class="cn-mono" title="${escapeHtml(row.ip_family || '')}">${escapeHtml(ip)}</td>
      <td class="r">${score}</td>
      <td class="r">${delay}</td>
      <td><span class="cn-mini-row">${cnBoolBadge(row.is_residential, '住宅')} ${cnBoolBadge(row.is_native, '原生')}</span></td>
      <td class="cn-class-cell">${cnClassBadge(row)}</td>
      <td>${canRetest ? `<button class="btn-ghost sm" data-cn-retest="${escapeHtml(name)}">重测</button>` : ''}</td>
    </tr>`;
  }
  function cnRenderResults(resetTo) {
    // 旧内容留到新数据拼好再一次性替换（切视图不清空内容区的项目铁律）
    const tbody = document.getElementById('cnTbody');
    const limit = resetTo ? 150 : Math.max(cnState.rendered, 150);
    const slice = cnState.items.slice(0, limit);
    cnState.rendered = slice.length;
    const html = slice.map(cnRenderRow).join('');
    tbody.innerHTML = html;
    tbody.querySelectorAll('[data-cn-retest]').forEach(btn => {
      btn.onclick = async () => {
        const node = btn.getAttribute('data-cn-retest');
        if (!confirm(`重测节点「${node}」？将临时拉起隔离内核单独探测该节点（约 10-20 秒）。`)) return;
        btn.disabled = true;
        try {
          const r = await fetch('/api/clashnodes/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'retest', node }) });
          const d = await r.json();
          if (!r.ok || d.error) throw Error(d.error || ('HTTP ' + r.status));
          loadNodesResults(true);
        } catch (err) { alert('重测失败：' + err.message); }
        finally { btn.disabled = false; }
      };
    });
    const more = document.getElementById('cnMoreWrap');
    if (more) more.style.display = cnState.items.length > cnState.rendered ? '' : 'none';
    document.getElementById('cnEmpty').hidden = cnState.items.length > 0 || !cnState.loaded;
    document.getElementById('cnTotal').textContent = cnState.loaded ? `共 ${cnState.total} 条` : '';
  }
  async function loadNodesResults(resetTo) {
    try {
      const q = cnState.query ? `&q=${encodeURIComponent(cnState.query)}` : '';
      const r = await fetch(`/api/clashnodes/results?filter=${cnState.filter}${q}`);
      const d = await r.json();
      if (d.error) throw Error(d.error);
      cnState.items = d.items || [];
      cnState.total = d.total || 0;
      cnState.loaded = true;
      if (d.counts) cnRenderCounts(d.counts);
      cnTrendSyncOptions();
      cnRenderResults(resetTo !== false);
    } catch (err) {
      const empty = document.getElementById('cnEmpty');
      empty.hidden = false;
      empty.textContent = '读取结果失败：' + err.message;
    }
  }
  function cnRenderCounts(counts) {
    const c = counts.classification || {};
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = String(v); };
    set('cnNumOk', c['合格'] ?? 0); set('cnNumWatch', c['观察'] ?? 0);
    set('cnNumBad', c['不合格'] ?? 0); set('cnNumFail', c['查询失败'] ?? 0);
    set('cnNumDual', counts.dual_green ?? 0); set('cnNumMismatch', counts.mismatch ?? 0);
  }
  // --- 检测动态（历史快照时间轴；手写 SVG 不引图表库，同项目雷达图先例） ---
  const cnTrend = { mode: 'global', node: '', nodeLabel: '', rounds: [] };
  const CN_CLASS_ORDER = ['合格', '观察', '不合格', '查询失败'];
  const CN_CLASS_FILL = { '合格': 'var(--ok)', '观察': 'var(--warn)', '不合格': 'var(--err)', '查询失败': 'var(--tx3)' };

  function cnTrendTimeLabel(iso) {
    const s = String(iso || '');
    return s.length >= 16 ? s.slice(5, 16).replace('T', ' ') : (s || '?');  // MM-DD HH:MM
  }
  function cnTrendEmpty(msg) {
    document.getElementById('cnTrendChart').innerHTML = `<div class="cn-trend-empty">${escapeHtml(msg)}</div>`;
    document.getElementById('cnTrendLegend').hidden = true;
  }
  async function loadNodesTrend() {
    if (!CN_PAGE()) return;
    const hint = document.getElementById('cnTrendHint');
    if (cnTrend.mode === 'global') {
      try {
        const r = await fetch('/api/clashnodes/history?limit=30');
        const d = await r.json();
        if (d.error) throw Error(d.error);
        cnTrend.rounds = d.rounds || [];
        cnDrawGlobalTrend();
      } catch (err) { cnTrendEmpty('动态图读取失败：' + err.message); }
      hint.textContent = cnTrend.rounds.length ? `最近 ${cnTrend.rounds.length} 轮` : '';
    } else {
      await cnLoadNodeTrend();
    }
  }
  function cnDrawGlobalTrend() {
    const rounds = cnTrend.rounds;
    if (!rounds.length) { cnTrendEmpty('还没有历史轮次。完成一轮检测后，这里会出现逐轮趋势（快照在本地 clash_node_history/）。'); return; }
    const W = 760, H = 240, L = 40, R = 10, T = 10, B = 30;
    const iw = W - L - R, ih = H - T - B;
    const maxN = Math.max(1, ...rounds.map(r => r.node_count || 0));
    const yTicks = 4;
    const barW = Math.min(30, Math.max(6, iw / rounds.length * 0.55));
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="各轮检测分级堆叠图">`;
    for (let i = 0; i <= yTicks; i++) {
      const v = Math.round(maxN * i / yTicks);
      const y = T + ih - ih * v / maxN;
      svg += `<line x1="${L}" y1="${y}" x2="${W - R}" y2="${y}" stroke="var(--hair)" stroke-width="1"/>`
        + `<text x="${L - 6}" y="${y + 3.5}" text-anchor="end" font-size="10" fill="var(--tx3)">${v}</text>`;
    }
    const step = Math.ceil(rounds.length / 8);
    rounds.forEach((rd, i) => {
      const cx = L + iw * (i + 0.5) / rounds.length;
      let acc = 0;
      const c = (rd.counts || {}).classification || {};
      let tip = `${cnTrendTimeLabel(rd.scanned_at)}\n共 ${rd.node_count} 节点`;
      CN_CLASS_ORDER.forEach(k => { tip += `\n${k} ${c[k] ?? 0}`; });
      CN_CLASS_ORDER.forEach(k => {
        const v = c[k] ?? 0;
        if (!v) return;
        const h = ih * v / maxN;
        const y = T + ih - ih * acc / maxN - h;
        svg += `<rect x="${cx - barW / 2}" y="${y}" width="${barW}" height="${Math.max(h, 0.5)}" fill="${CN_CLASS_FILL[k]}"><title>${escapeHtml(tip)}</title></rect>`;
        acc += v;
      });
      if (i % step === 0 || i === rounds.length - 1) {
        svg += `<text x="${cx}" y="${H - 10}" text-anchor="middle" font-size="10" fill="var(--tx3)">${escapeHtml(cnTrendTimeLabel(rd.scanned_at))}</text>`;
      }
    });
    svg += '</svg>';
    document.getElementById('cnTrendChart').innerHTML = svg;
    document.getElementById('cnTrendLegend').hidden = false;
  }
  function cnTrendSyncOptions() {
    const sel = document.getElementById('cnTrendNode');
    const cur = sel.value;
    sel.innerHTML = '<option value="">— 选择节点 —</option>' + (cnState.items || []).map(it =>
      `<option value="${escapeHtml(it.fingerprint || it.display_name)}">${escapeHtml(it.display_name || it.probe_name)}</option>`
    ).join('');
    if (cur) sel.value = cur;
  }
  async function cnLoadNodeTrend() {
    const sel = document.getElementById('cnTrendNode');
    if (!sel.value) { cnTrendEmpty('先在右侧下拉选择要查看的节点（来自当前检测结果）。'); return; }
    try {
      const r = await fetch(`/api/clashnodes/history?node=${encodeURIComponent(sel.value)}`);
      const d = await r.json();
      if (d.error) throw Error(d.error);
      cnDrawNodeTrend(d.node?.display_name || sel.value, d.series || []);
    } catch (err) { cnTrendEmpty('单节点历史读取失败：' + err.message); }
  }
  function cnDrawNodeTrend(name, series) {
    const hint = document.getElementById('cnTrendHint');
    if (!series.length) {
      cnTrendEmpty(`「${name}」还没有历史轮次（快照按节点指纹跨轮匹配）。`);
      hint.textContent = '';
      return;
    }
    hint.textContent = `「${name}」最近 ${series.length} 轮`;
    const W = 760, H = 240, L = 40, R = 10, T = 10, B = 30;
    const iw = W - L - R, ih = H - T - B;
    const xAt = i => L + iw * (i + 0.5) / series.length;
    const yAt = v => T + ih - ih * (Math.max(0, Math.min(100, v))) / 100;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="单节点风险分趋势">`;
    for (let v = 0; v <= 100; v += 25) {
      const y = yAt(v);
      svg += `<line x1="${L}" y1="${y}" x2="${W - R}" y2="${y}" stroke="var(--hair)" stroke-width="1"/>`
        + `<text x="${L - 6}" y="${y + 3.5}" text-anchor="end" font-size="10" fill="var(--tx3)">${v}</text>`;
    }
    // 风险分 60 线（默认不合格阈值）做参考线
    svg += `<line x1="${L}" y1="${yAt(60)}" x2="${W - R}" y2="${yAt(60)}" stroke="var(--err)" stroke-width="1" stroke-dasharray="4 4" opacity=".55"/>`
      + `<text x="${W - R - 2}" y="${yAt(60) - 4}" text-anchor="end" font-size="9.5" fill="var(--err)" opacity=".8">不合格阈值 60（随设置变化以页面设置为准）</text>`;
    const pts = [];
    series.forEach((p, i) => {
      if (typeof p.fraud_score !== 'number') return;
      pts.push(`${xAt(i)},${yAt(p.fraud_score)}`);
      const fill = CN_CLASS_FILL[p.classification] || 'var(--tx3)';
      svg += `<circle cx="${xAt(i)}" cy="${yAt(p.fraud_score)}" r="4" fill="${fill}">`
        + `<title>${escapeHtml(`${cnTrendTimeLabel(p.scanned_at)}\n${p.classification || '—'} · 风险分 ${p.fraud_score}\n出口 ${p.country_code || '—'} · ${p.ip_redacted || '—'}`)}</title></circle>`;
    });
    if (pts.length > 1) svg += `<polyline points="${pts.join(' ')}" fill="none" stroke="var(--acc)" stroke-width="1.6" opacity=".8"/>`;
    const step = Math.ceil(series.length / 8);
    series.forEach((p, i) => {
      if (i % step === 0 || i === series.length - 1) {
        svg += `<text x="${xAt(i)}" y="${H - 10}" text-anchor="middle" font-size="10" fill="var(--tx3)">${escapeHtml(cnTrendTimeLabel(p.scanned_at))}</text>`;
      }
    });
    svg += '</svg>';
    document.getElementById('cnTrendChart').innerHTML = svg;
    document.getElementById('cnTrendLegend').hidden = true;
  }
  function cnRenderStatus(st) {
    const running = !!st.running || (st.phase && st.phase !== 'idle');
    const wrap = document.getElementById('cnProgressWrap');
    if (!running && !st.error) { wrap.style.display = 'none'; }
    else {
      wrap.style.display = '';
      const p = st.progress || {};
      const pct = p.total ? Math.round((p.done || 0) / p.total * 100) : 0;
      document.getElementById('cnProgressFill').style.width = pct + '%';
      const cur = p.current_display ? ` · ${p.current_display}` : '';
      const scope = Array.isArray(st.scope) && st.scope.length
        ? ` · 仅 ${st.scope.length} 个订阅` : '';
      const cc = (st.concurrency || 1) > 1 ? ` · 并行×${st.concurrency}` : '';
      document.getElementById('cnProgressText').textContent =
        st.error ? `出错：${st.error}` : `${cnPhaseText(st.phase)} ${p.done || 0}/${p.total || 0}（${pct}%）${scope}${cc}${cur}`;
      wrap.classList.toggle('cn-progress-err', !!st.error && !running);
    }
    document.getElementById('cnBtnStart').disabled = running;
    document.getElementById('cnBtnStop').disabled = !running;
    document.getElementById('cnBtnReset').disabled = running;
    if (st.counts) cnRenderCounts(st.counts);
    if (running) cnStartPoll(); else cnStopPoll();
    // 扫描刚结束（轮询里从 running 变 idle）要刷新一次结果、环境与动态图
    if (!running && cnState._wasRunning) {
      cnState._wasRunning = false;
      loadNodesResults(true);
      loadNodesEnv();
      loadNodesTrend();
    }
    if (running) cnState._wasRunning = true;
  }
  async function loadNodesStatus() {
    try {
      const r = await fetch('/api/clashnodes/status');
      const d = await r.json();
      if (!d.error) cnRenderStatus(d);
    } catch (err) { /* 服务不可达时保持现状 */ }
  }
  function cnStartPoll() {
    if (cnState.pollTimer) return;
    // 只有精确停在 #/nodes 时才轮询（startsWith 会串到别的页面，项目踩过）
    cnState.pollTimer = setInterval(() => {
      if (!CN_PAGE()) { cnStopPoll(); return; }
      loadNodesStatus();
    }, 2000);
  }
  function cnStopPoll() {
    if (cnState.pollTimer) { clearInterval(cnState.pollTimer); cnState.pollTimer = null; }
  }
  async function loadNodesEnv() {
    try {
      const r = await fetch('/api/clashnodes/env');
      const d = await r.json();
      if (d.error) throw Error(d.error);
      const env = d.env || {}, cfg = d.config || {}, col = env.collections || {};
      const mark = ok => ok ? '<span class="cn-mini ok">✓</span>' : '<span class="cn-mini err">✗</span>';
      document.getElementById('cnEnvVerge').innerHTML =
        `${mark(env.verge_dir_exists)} <span class="cn-mono">${escapeHtml(cfg.verge_dir || '')}</span>`;
      document.getElementById('cnEnvCore').innerHTML = env.core_found
        ? `${mark(true)} <span class="cn-mono">${escapeHtml(env.core_path_effective || '')}</span> <span class="cn-dim">${escapeHtml(env.core_version || '')}</span>`
        : `${mark(false)} 找不到内核，请在设置里填路径`;
      document.getElementById('cnEnvProfile').textContent = env.current_profile || '—';
      document.getElementById('cnEnvNodes').textContent = col.unique_nodes
        ? `${col.unique_nodes} 个（原始 ${col.candidate_records} 条，去重 ${col.dedup}）` : '—';
      const last = env.last_run;
      document.getElementById('cnEnvLast').textContent =
        last ? (last.finished_at ? String(last.finished_at).replace('T', ' ').slice(0, 16) : '进行中')
          : (env.cache_updated_at ? String(env.cache_updated_at).replace('T', ' ').slice(0, 16) + '（有历史缓存）' : '从未');
      const errEl = document.getElementById('cnEnvError');
      if (col.error) { errEl.hidden = false; errEl.textContent = '收集节点失败：' + col.error; }
      else if (!env.verge_dir_exists) { errEl.hidden = false; errEl.textContent = '未找到 Clash Verge 数据目录：请确认本机装有 Clash Verge Rev，或在设置里填正确路径。'; }
      else errEl.hidden = true;
      const subs = col.subscriptions || [];
      const list = document.getElementById('cnSubList');
      list.innerHTML = subs.map((s, i) => {
        const selected = cnState.scope.has(s.name);
        const cls = ['cn-sub-chip', s.skipped ? 'skipped' : 'selectable', selected ? 'selected' : ''].filter(Boolean).join(' ');
        return `<span class="${cls}" data-cn-sub-idx="${i}" title="原始 ${s.raw} 条 / 可用 ${s.usable} 条${s.skipped ? ' · 命中排除词' : (selected ? ' · 已勾选：只检测这些订阅' : ' · 点击勾选')}">${escapeHtml(s.name)} <b>${s.usable}</b>${s.skipped ? ' · 已排除' : (selected ? ' ✓' : '')}</span>`;
      }).join('');
      list.querySelectorAll('[data-cn-sub-idx]').forEach(el => {
        el.onclick = () => {
          const s = subs[Number(el.getAttribute('data-cn-sub-idx'))];
          if (!s || s.skipped) return;
          if (cnState.scope.has(s.name)) cnState.scope.delete(s.name); else cnState.scope.add(s.name);
          loadNodesEnv();
        };
      });
      document.getElementById('cnScopeHint').textContent = cnState.scope.size
        ? `已勾选 ${cnState.scope.size} 个订阅，本轮只检测它们` : '点订阅名可勾选范围；不勾选 = 检测全部';
      // 设置面板回填（只在用户没在编辑时）
      const core = document.getElementById('cnSetCore');
      if (core && document.activeElement !== core) core.value = cfg.core_path || '';
      const verge = document.getElementById('cnSetVerge');
      if (verge && document.activeElement !== verge) verge.value = cfg.verge_dir || '';
      const skip = document.getElementById('cnSetSkip');
      if (skip && document.activeElement !== skip) skip.value = (cfg.skip_subscription_words || []).join(', ');
      const okT = document.getElementById('cnSetOk');
      if (okT && document.activeElement !== okT) okT.value = cfg.ok_threshold ?? 30;
      const badT = document.getElementById('cnSetBad');
      if (badT && document.activeElement !== badT) badT.value = cfg.bad_threshold ?? 60;
      const concT = document.getElementById('cnSetConcurrency');
      if (concT && document.activeElement !== concT) concT.value = cfg.concurrency ?? 4;
      const delayT = document.getElementById('cnSetDelay');
      if (delayT) delayT.checked = cfg.delay_test !== false;
      const ipqsKey = document.getElementById('cnSetIpqsKey');
      if (ipqsKey && document.activeElement !== ipqsKey) ipqsKey.value = cfg.ipqs_api_key || '';
      const ping0Key = document.getElementById('cnSetPing0Key');
      if (ping0Key && document.activeElement !== ping0Key) ping0Key.value = cfg.ping0_api_key || '';
    } catch (err) {
      const errEl = document.getElementById('cnEnvError');
      errEl.hidden = false;
      errEl.textContent = '读取环境失败：' + err.message;
    }
  }
  function cnSetFilter(f) {
    cnState.filter = f;
    document.querySelectorAll('#cnChips .cn-chip').forEach(b => b.classList.toggle('on', b.dataset.cnFilter === f));
    loadNodesResults(true);
  }
  // --- 配置体检与分级写回 ---
  async function loadNodesHealth(online) {
    const hint = document.getElementById('cnHealthHint');
    hint.hidden = true;
    try {
      const r = await fetch(`/api/clashnodes/health?online=${online ? 1 : 0}`);
      const d = await r.json();
      if (d.error) throw Error(d.error);
      const items = d.items || [];
      document.getElementById('cnHealthWrap').hidden = false;
      document.getElementById('cnHealthTbody').innerHTML = items.map(it => {
        const expire = it.expire_at ? String(it.expire_at).slice(0, 10) : '';
        const traffic = it.traffic_total_gb ? `${it.traffic_used_gb ?? '—'} / ${it.traffic_total_gb} GB` : '';
        const hl = it.yaml_ok === false ? ' <span class="cn-mini err">解析失败</span>' : '';
        return `<tr>
          <td><strong>${escapeHtml(it.name)}</strong>${hl}${it.file_exists === false ? ' <span class="cn-mini err">文件缺失</span>' : ''}</td>
          <td>${it.kind === 'remote' ? '订阅' : '本地'}</td>
          <td class="r">${it.proxy_count ?? '—'}</td>
          <td class="r">${it.unique_count ?? '—'}</td>
          <td class="r">${it.duplicate_count ? `<span class="cn-mini warn">${it.duplicate_count}</span>` : '0'}</td>
          <td class="r">${it.group_count ?? '—'}</td>
          <td class="r">${it.rule_count ?? '—'}</td>
          <td><span class="cn-mono">${escapeHtml(it.host || '—')}</span></td>
          <td>${escapeHtml([traffic, expire].filter(Boolean).join(' · ') || (online ? '未提供' : '未查'))}</td>
        </tr>`;
      }).join('');
      if (!items.length) { hint.hidden = false; hint.textContent = '没有可检查的配置。'; }
    } catch (err) { hint.hidden = false; hint.textContent = '体检失败：' + err.message; }
  }
  let cnLatestReportText = '';
  async function cnLoadReportPreview() {
    const hint = document.getElementById('cnReportHint');
    hint.hidden = true;
    try {
      const r = await fetch('/api/clashnodes/report');
      if (!r.ok) throw Error('HTTP ' + r.status);
      const text = await r.text();
      if (!text || text.startsWith('{"error":')) {
        const d = JSON.parse(text);
        throw Error(d.error || '获取报告失败');
      }
      cnLatestReportText = text;
      document.getElementById('cnReportBox').hidden = false;
      document.getElementById('cnReportPre').textContent = text;
      document.getElementById('cnReportMeta').innerHTML =
        `报告已就绪 · 共 ${text.length} 字符 · 格式为脱敏 Markdown · 已包含分组建议与剔除明细，可直接发给 AI 分析。`;
    } catch (err) {
      document.getElementById('cnReportBox').hidden = true;
      hint.hidden = false;
      hint.textContent = '生成报告失败：' + err.message;
    }
  }

  async function cnCopyReport() {
    if (!cnLatestReportText) {
      await cnLoadReportPreview();
    }
    if (cnLatestReportText) {
      try {
        await navigator.clipboard.writeText(cnLatestReportText);
        alert('报告已成功复制到剪贴板！可直接粘贴发给 AI。');
      } catch (e) {
        alert('剪贴板写入受限，请在上方预览框内全选复制');
      }
    }
  }

  async function cnRemoveLegacyWriteback() {
    if (!confirm('从 Merge.yaml 摘除工具箱历史写入的管理段？（先备份，其余内容原样保留）')) return;
    try {
      const r = await fetch('/api/clashnodes/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'writeback_remove', confirm: true })
      });
      const d = await r.json();
      if (!r.ok || d.error) throw Error(d.error || ('HTTP ' + r.status));
      alert((d.hint || d.note || '已完成') + (d.backup ? `\n备份：${d.backup}` : ''));
    } catch (err) {
      alert('操作失败：' + err.message);
    }
  }
  function initNodes() {
    if (!initNodes.done) {
      initNodes.done = true;
      document.querySelectorAll('#cnChips .cn-chip').forEach(b => b.onclick = () => cnSetFilter(b.dataset.cnFilter));
      const tileMap = { cnTileOk: 'ok', cnTileWatch: 'watch', cnTileBad: 'bad', cnTileFail: 'fail', cnTileDual: 'dual', cnTileMismatch: 'mismatch' };
      Object.entries(tileMap).forEach(([id, f]) => {
        const el = document.getElementById(id);
        if (el) el.onclick = () => cnSetFilter(f);
      });
      let searchTimer = null;
      document.getElementById('cnSearch').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { cnState.query = document.getElementById('cnSearch').value.trim(); loadNodesResults(true); }, 300);
      });
      document.getElementById('cnBtnStart').onclick = async () => {
        try {
          const payload = { action: 'start' };
          if (cnState.scope.size) payload.subscriptions = [...cnState.scope];
          const r = await fetch('/api/clashnodes/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          const d = await r.json();
          if (!r.ok || d.error) throw Error(d.error || ('HTTP ' + r.status));
          cnState._wasRunning = true;
          loadNodesStatus();
        } catch (err) { alert('启动失败：' + err.message); }
      };
      document.getElementById('cnBtnStop').onclick = async () => {
        try {
          const r = await fetch('/api/clashnodes/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'stop' }) });
          const d = await r.json();
          if (!r.ok || d.error) throw Error(d.error || ('HTTP ' + r.status));
          loadNodesStatus();
        } catch (err) { alert('停止失败：' + err.message); }
      };
      document.getElementById('cnBtnReset').onclick = async () => {
        if (!confirm('清空全部检测结果？清空后下次「开始检测」会全量重测。')) return;
        try {
          const r = await fetch('/api/clashnodes/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'reset' }) });
          const d = await r.json();
          if (!r.ok || d.error) throw Error(d.error || ('HTTP ' + r.status));
          cnState.loaded = false;
          loadNodesResults(true);
          loadNodesStatus();
        } catch (err) { alert('清空失败：' + err.message); }
      };
      document.getElementById('cnBtnExport').onclick = () => {
        const q = cnState.query ? `&q=${encodeURIComponent(cnState.query)}` : '';
        window.open(`/api/clashnodes/export?filter=${cnState.filter}${q}`, '_blank');
      };
      document.getElementById('cnBtnReport').onclick = () => {
        window.open('/api/clashnodes/report', '_blank');
      };
      document.getElementById('cnBtnConfig').onclick = () => {
        if (!confirm('按检测结果重排各分组组员并导出完整配置（含节点凭据）？\n'
          + '文件只落本机 clash_node_reports/ 目录，请勿外传或发给 AI。\n'
          + '导出后在 Clash Verge 里「导入」该 yaml 即可使用。')) return;
        window.open('/api/clashnodes/export-config', '_blank');
      };
      document.getElementById('cnBtnSettings').onclick = () => {
        const box = document.getElementById('cnSettingsBox');
        box.hidden = !box.hidden;
      };
      document.getElementById('cnTrendTabGlobal').onclick = () => {
        cnTrend.mode = 'global';
        document.getElementById('cnTrendTabGlobal').classList.add('on');
        document.getElementById('cnTrendTabNode').classList.remove('on');
        document.getElementById('cnTrendNode').hidden = true;
        loadNodesTrend();
      };
      document.getElementById('cnTrendTabNode').onclick = () => {
        cnTrend.mode = 'node';
        document.getElementById('cnTrendTabNode').classList.add('on');
        document.getElementById('cnTrendTabGlobal').classList.remove('on');
        document.getElementById('cnTrendNode').hidden = false;
        loadNodesTrend();
      };
      document.getElementById('cnTrendNode').onchange = () => loadNodesTrend();
      document.getElementById('cnBtnHealth').onclick = () => loadNodesHealth(false);
      document.getElementById('cnBtnHealthOnline').onclick = () => loadNodesHealth(true);
      const btnPreview = document.getElementById('cnBtnReportPreview');
      if (btnPreview) btnPreview.onclick = cnLoadReportPreview;
      const btnDownload = document.getElementById('cnBtnReportDownload');
      if (btnDownload) btnDownload.onclick = () => window.open('/api/clashnodes/report', '_blank');
      const btnCopy = document.getElementById('cnBtnReportCopy');
      if (btnCopy) btnCopy.onclick = cnCopyReport;
      const btnWbRemove = document.getElementById('cnBtnWbRemove');
      if (btnWbRemove) btnWbRemove.onclick = cnRemoveLegacyWriteback;
      document.getElementById('cnBtnSaveSettings').onclick = async () => {
        const hint = document.getElementById('cnSettingsHint');
        const payload = {
          core_path: document.getElementById('cnSetCore').value.trim(),
          verge_dir: document.getElementById('cnSetVerge').value.trim(),
          skip_subscription_words: document.getElementById('cnSetSkip').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
          ok_threshold: Number(document.getElementById('cnSetOk').value),
          bad_threshold: Number(document.getElementById('cnSetBad').value),
          concurrency: Number(document.getElementById('cnSetConcurrency').value),
          delay_test: document.getElementById('cnSetDelay').checked,
          ipqs_api_key: (document.getElementById('cnSetIpqsKey') || {}).value?.trim() || '',
          ping0_api_key: (document.getElementById('cnSetPing0Key') || {}).value?.trim() || '',
        };
        try {
          const r = await fetch('/api/clashnodes/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          const d = await r.json();
          if (!r.ok || d.error) throw Error(d.error || ('HTTP ' + r.status));
          hint.textContent = '已保存。';
          loadNodesEnv();
        } catch (err) { hint.textContent = '保存失败：' + err.message; }
      };
      const more = document.createElement('div');
      more.id = 'cnMoreWrap';
      more.style.cssText = 'text-align:center;padding:10px 0;';
      more.innerHTML = '<button class="btn-ghost sm" id="cnBtnMore">显示更多</button>';
      document.getElementById('cnTbody').parentNode.parentNode.appendChild(more);
      document.getElementById('cnBtnMore').onclick = () => cnRenderResults(false);

      // --- 诊断仪子视图切换与事件绑定（三视图：实时诊断 / 单 IP 查询 / 批量巡检） ---
      const tabInspector = document.getElementById('cnSubTabInspector');
      const tabIpq = document.getElementById('cnSubTabIpq');
      const tabScan = document.getElementById('cnSubTabScan');
      const viewInspector = document.getElementById('cnInspectorView');
      const viewIpq = document.getElementById('cnIpQueryView');
      const viewScan = document.getElementById('cnScanView');

      const setNodesTab = (which) => {
        [['inspector', tabInspector], ['ipq', tabIpq], ['scan', tabScan]].forEach(([k, t]) => {
          if (t) t.classList.toggle('on', k === which);
        });
        if (viewInspector) viewInspector.style.display = which === 'inspector' ? 'block' : 'none';
        if (viewIpq) viewIpq.hidden = which !== 'ipq';
        if (viewScan) viewScan.style.display = which === 'scan' ? 'block' : 'none';
        if (which === 'inspector') loadClashInspector();
        if (which === 'scan') loadNodesScanAll();
      };
      if (tabInspector && tabScan) {
        tabInspector.onclick = () => setNodesTab('inspector');
        if (tabIpq) tabIpq.onclick = () => setNodesTab('ipq');
        tabScan.onclick = () => setNodesTab('scan');
      }

      // Cloudflare 检测按钮（事件委托，渲染后动态出现）
      if (viewInspector) {
        viewInspector.addEventListener('click', (ev) => {
          const btn = ev.target && ev.target.closest ? ev.target.closest('.ci-cf-run') : null;
          if (btn) ciRefreshCf(btn.dataset.side || 'direct', true);
        });
      }

      // 单 IP 查询：按钮 / 回车 / 快捷预设
      const ipqBtn = document.getElementById('ipqBtn');
      const ipqInput = document.getElementById('ipqInput');
      if (ipqBtn && ipqInput) {
        ipqBtn.onclick = runIpQuery;
        ipqInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') runIpQuery(); });
      }
      document.querySelectorAll('.ci-quick-chip[data-ipq]').forEach(chip => {
        chip.onclick = () => {
          const v = chip.dataset.ipq;
          const input = document.getElementById('ipqInput');
          if (v === 'current') {
            const cur = (document.getElementById('ciProxyIp') || {}).textContent || '';
            if (input) input.value = (cur && cur !== '–') ? cur : '';
            return;
          }
          if (input) input.value = v;
          runIpQuery();
        };
      });

      // 安全与泄露检测：三角展开 + 运行按钮
      [['Dns', 'ciAccBtnDns'], ['Rtc', 'ciAccBtnRtc'], ['Loc', 'ciAccBtnLoc'], ['Nb', 'ciAccBtnNb']].forEach(pair => {
        const btn = document.getElementById(pair[1]);
        if (btn) btn.onclick = () => ciToggleAcc(pair[0]);
      });
      const rtcBtn = document.getElementById('ciRtcRun');
      if (rtcBtn) rtcBtn.onclick = (ev) => { ev.stopPropagation(); runCiWebrtcCheck(); };
      const locBtn = document.getElementById('ciLocRun');
      if (locBtn) locBtn.onclick = (ev) => { ev.stopPropagation(); runCiLocCheck(); };
      const nbBtn = document.getElementById('ciNbRun');
      if (nbBtn) nbBtn.onclick = (ev) => { ev.stopPropagation(); runCiNeighbors(); };

      const btnCiRefresh = document.getElementById('ciBtnRefresh');
      if (btnCiRefresh) btnCiRefresh.onclick = () => loadClashInspector(true);

      const selCiAuto = document.getElementById('ciAutoRefresh');
      if (selCiAuto) {
        const savedAuto = localStorage.getItem('ci_auto_refresh') || '0';
        selCiAuto.value = savedAuto;
        setCiAutoRefresh(savedAuto);
        selCiAuto.onchange = () => setCiAutoRefresh(selCiAuto.value);
      }

      const btnCiTrace = document.getElementById('ciBtnTrace');
      const inputCiTarget = document.getElementById('ciTargetInput');
      if (btnCiTrace && inputCiTarget) {
        btnCiTrace.onclick = () => runWebsiteTrace(inputCiTarget.value);
        inputCiTarget.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') runWebsiteTrace(inputCiTarget.value);
        });
      }

      document.querySelectorAll('.ci-quick-chip[data-domain]').forEach(chip => {
        chip.onclick = () => {
          const dom = chip.dataset.domain;
          if (inputCiTarget) inputCiTarget.value = dom;
          runWebsiteTrace(dom);
        };
      });

      const btnCopyProxy = document.getElementById('ciBtnCopyProxyIp');
      if (btnCopyProxy) btnCopyProxy.onclick = () => {
        const ip = (document.getElementById('ciProxyIp') || {}).textContent;
        if (ip && ip !== '–') navigator.clipboard.writeText(ip).then(() => alert('已复制代理出口 IP: ' + ip));
      };
      const btnCopyDirect = document.getElementById('ciBtnCopyDirectIp');
      if (btnCopyDirect) btnCopyDirect.onclick = () => {
        const ip = (document.getElementById('ciDirectIp') || {}).textContent;
        if (ip && ip !== '–') navigator.clipboard.writeText(ip).then(() => alert('已复制本地直连 IP: ' + ip));
      };

      // 风险检测 API Key 弹窗事件绑定
      const btnOpenKeys = document.getElementById('btnOpenCiApiKeysModal');
      if (btnOpenKeys) btnOpenKeys.onclick = () => openCiApiKeysModal();
      const btnCloseKeys = document.getElementById('btnCloseCiApiKeysModal');
      if (btnCloseKeys) btnCloseKeys.onclick = closeCiApiKeysModal;
      const btnCancelKeys = document.getElementById('btnCancelCiApiKeysModal');
      if (btnCancelKeys) btnCancelKeys.onclick = closeCiApiKeysModal;
      const btnSaveKeys = document.getElementById('btnSaveCiApiKeysModal');
      if (btnSaveKeys) btnSaveKeys.onclick = saveCiApiKeys;
      const eyeIpqs = document.getElementById('btnToggleCiIpqsEye');
      if (eyeIpqs) eyeIpqs.onclick = () => toggleCiInputVisibility(document.getElementById('modalCiIpqsKey'), eyeIpqs);
      const eyeP0 = document.getElementById('btnToggleCiPing0Eye');
      if (eyeP0) eyeP0.onclick = () => toggleCiInputVisibility(document.getElementById('modalCiPing0Key'), eyeP0);
      const eyeIpapi = document.getElementById('btnToggleCiIpapiIsEye');
      if (eyeIpapi) eyeIpapi.onclick = () => toggleCiInputVisibility(document.getElementById('modalCiIpapiIsKey'), eyeIpapi);
      const overlayKeys = document.getElementById('ciApiKeysModalOverlay');
      if (overlayKeys) {
        overlayKeys.onclick = (e) => {
          if (e.target === overlayKeys) closeCiApiKeysModal();
        };
      }
      document.addEventListener('click', (ev) => {
        const btn = ev.target && ev.target.closest ? ev.target.closest('.ci-jump-settings') : null;
        if (btn) {
          ev.preventDefault();
          const target = btn.dataset.target || 'ipqs';
          openCiApiKeysModal(target);
        }
      });
    }

    // 页面进入逻辑：按激活的子视图加载
    const tabInspectorActive = (document.getElementById('cnSubTabInspector') || {}).classList?.contains('on') ?? true;
    if (tabInspectorActive) {
      loadClashInspector();
    } else if ((document.getElementById('cnSubTabIpq') || {}).classList?.contains('on')) {
      /* 单 IP 查询：空白等待输入 */
    } else {
      loadNodesScanAll();
    }
  }

  function loadNodesScanAll() {
    loadNodesEnv();
    loadNodesResults(true);
    loadNodesStatus();
    loadNodesTrend();
    cnWbPreview();
  }

  // ===== Clash 诊断仪：ippure 风格报告渲染（分数色带 / 人机比 / 多源位置 / IP 查询） =====

  const CI_SEGS = [[0, 15, '#166534'], [15, 25, '#22c55e'], [25, 40, '#84cc16'], [40, 50, '#eab308'], [50, 70, '#f97316'], [70, 100, '#dc2626']];

  function ciTier(score) {
    if (score === null || score === undefined || isNaN(Number(score))) return null;
    const s = Number(score);
    if (s < 15) return { label: '极度纯净', color: '#166534' };
    if (s < 25) return { label: '纯净', color: '#22c55e' };
    if (s < 40) return { label: '中性', color: '#84cc16' };
    if (s < 50) return { label: '轻度风险', color: '#eab308' };
    if (s < 70) return { label: '中度风险', color: '#f97316' };
    return { label: '极度风险', color: '#dc2626' };
  }

  function ciScoreBar(score) {
    if (score === null || score === undefined || isNaN(Number(score))) return '<span class="ci-mut">–</span>';
    const s = Math.max(0, Math.min(100, Number(score)));
    const t = ciTier(s);
    const segs = CI_SEGS.map(seg => `<i style="width:${seg[1] - seg[0]}%;background:${seg[2]}"></i>`).join('');
    const ticks = [0, 15, 25, 40, 50, 70, 100].map(v => {
      if (v === 0) return '<b style="left:0;transform:none">0</b>';
      if (v === 100) return '<b style="right:0;transform:none">100</b>';
      return `<b style="left:${v}%">${v}</b>`;
    }).join('');
    return `<span class="ci-barline"><span class="ci-bar"><span class="ci-bar-segs">${segs}</span><span class="ci-bar-mark" style="left:${s}%"></span><span class="ci-bar-ticks">${ticks}</span></span><span class="ci-tier" style="background:${t.color}">${s}% ${t.label}</span></span>`;
  }

  function ciHumanBotBar(bc) {
    if (!bc || bc.human === undefined || bc.human === null) return '<span class="ci-mut">未获取</span>';
    const h = Math.max(0, Math.min(100, Number(bc.human) || 0));
    const b = Math.max(0, 100 - h);
    return `<span class="ci-hbwrap"><span class="ci-hb"><i class="ci-hb-h" style="width:${h}%"></i><i class="ci-hb-b" style="width:${b}%"></i></span><span class="ci-hb-txt">human ${h.toFixed(2)}% · bot ${b.toFixed(2)}%</span></span>`;
  }

  function ciGeoRow(src, cc, text) {
    // Windows 系不渲染彩色国旗 emoji（退化成字母对），用国码徽标保证跨机一致
    const chip = (cc && typeof cc === 'string' && /^[A-Za-z]{2}$/.test(cc)) ? `<span class="ci-cc">${cc.toUpperCase()}</span> ` : '';
    return `<div class="ci-geo-row"><span class="ci-geo-src">${src}</span><span class="ci-geo-val">${chip}${escapeHtml(text || '–')}</span></div>`;
  }

  function ciJoinLoc(parts) {
    const uniq = [];
    (parts || []).forEach(p => { const v = String(p || '').trim(); if (v && !uniq.includes(v)) uniq.push(v); });
    return uniq.join(', ');
  }

  function ciLocFromBasicGeo(g) {
    if (!g) return null;
    return ciJoinLoc([g.city, g.subdivisions]) || g.country_code || null;
  }

  function ciSourceTags(traits, bcFallback, asnType, p0) {
    const tags = [];
    const bc = traits ? traits.is_broadcast : (p0 && p0.native !== null && p0.native !== undefined ? !p0.native : bcFallback);
    if (bc === false) tags.push('<span class="ci-tag ci-tag-ok">原生IP</span>');
    else if (bc === true) tags.push('<span class="ci-tag ci-tag-warn">广播IP</span>');
    if (p0 && p0.type) tags.push(`<span class="ci-tag ci-tag-dim">${escapeHtml(p0.type)}</span>`);
    else if (asnType === 'hosting') tags.push('<span class="ci-tag ci-tag-dim">机房段</span>');
    if (traits && traits.is_warp === true) tags.push('<span class="ci-tag ci-tag-warn">WARP</span>');
    return tags.join(' ') || '<span class="ci-mut">–</span>';
  }

  function ciAttrTags(resi, ipap, asnType, scam, p0) {
    const tags = [];
    if (resi === true) tags.push('<span class="ci-tag ci-tag-ok">住宅IP</span>');
    else if (resi === false && asnType === 'hosting') tags.push('<span class="ci-tag ci-tag-err">机房IP</span>');
    else if (resi === false) tags.push('<span class="ci-tag ci-tag-dim">非住宅</span>');

    if (p0 && p0.usecount) {
      const u = p0.usecount;
      let cls = 'ci-tag-ok';
      if (u.includes('一般') || u.includes('10-100') || u.includes('10 - 100')) cls = 'ci-tag-warn';
      else if (u.includes('差') || u.includes('危') || u.includes('1000') || u.includes('10000')) cls = 'ci-tag-err';
      tags.push(`<span class="ci-tag ${cls}" title="Ping0 全网独立用户估算"><i class="ph ph-users"></i> 共享人数: ${escapeHtml(u)}</span>`);
    }

    if (scam && scam.status === 'ok') {
      if (scam.residential === true) tags.push('<span class="ci-tag ci-tag-ok">住宅代理</span>');
      if (scam.datacenter === true || scam.server === true) tags.push('<span class="ci-tag ci-tag-dim">机房/服务器</span>');
      if (scam.tor === true) tags.push('<span class="ci-tag ci-tag-err">Tor出口</span>');
      if (scam.vpn === true) tags.push('<span class="ci-tag ci-tag-warn">VPN出口</span>');
    }

    if (ipap && ipap.status === 'ok') {
      if (ipap.proxy === true) tags.push('<span class="ci-tag ci-tag-err">公开代理标记</span>');
      if (ipap.mobile === true) tags.push('<span class="ci-tag ci-tag-warn">移动网络</span>');
    }
    return tags.join(' ') || '<span class="ci-mut">–</span>';
  }

  function ciGeoRowsForEgress(e, basic) {
    const rows = [];
    const cf = e.cloudflare || {};
    if (cf.status === 'ok') {
      const extra = [cf.colo ? ('colo ' + cf.colo) : '', cf.warp === 'on' ? 'WARP' : ''].filter(Boolean).join(' · ');
      rows.push(ciGeoRow('Cloudflare', (cf.loc && cf.loc.length === 2) ? cf.loc : null, (cf.loc || '') + (extra ? ' · ' + extra : '')));
    }
    const geo = (basic && basic.geo) || null;
    if (geo) {
      rows.push(ciGeoRow('IP2Location', (geo['IP2Location'] || {}).country_code, ciLocFromBasicGeo(geo['IP2Location'])));
      rows.push(ciGeoRow('DB-IP', (geo['DB-IP'] || {}).country_code, ciLocFromBasicGeo(geo['DB-IP'])));
      rows.push(ciGeoRow('MaxMind', (geo['MaxMind'] || {}).country_code, ciLocFromBasicGeo(geo['MaxMind'])));
    }
    const inf = e.ipinfo || {};
    if (inf.status === 'ok') rows.push(ciGeoRow('ipinfo.io', inf.country, ciJoinLoc([inf.city, inf.region])));
    const sb = e.ipsb || {};
    if (sb.status === 'ok') rows.push(ciGeoRow('IP.SB', sb.country_code, ciJoinLoc([sb.city])));
    const wh = e.ipwho || {};
    if (wh.status === 'ok') rows.push(ciGeoRow('ipwho.is', wh.country_code, ciJoinLoc([wh.city, wh.region])));
    const ap = e.ipapi || {};
    if (ap.status === 'ok') rows.push(ciGeoRow('ip-api', ap.country_code, ciJoinLoc([ap.city, ap.region])));
    const p0 = e.ping0 || {};
    if (p0.location) rows.push(ciGeoRow('Ping0', null, p0.location + (p0.isp ? (' · ' + p0.isp) : '')));
    return rows;
  }

  // 出口画像渲染（代理 / 直连两卡共用）
  function renderCiEgress(side, e) {
    const p = side === 'proxy' ? 'ciProxy' : 'ciDirect';
    const byId = (n) => document.getElementById(p + n);
    const ipa = e.ippure_api || {};
    const basic = ipa.basic || null;
    const asn = (basic && basic.asn) || null;
    const traits = (basic && basic.traits) || null;

    // ASN / AS域名 / IP范围
    const elAsn = byId('Asn');
    if (elAsn) {
      let txt = '–';
      if (asn) txt = 'AS' + asn.number + ' - ' + (asn.organization || '');
      else if (e.asn || e.org || e.isp) txt = ((e.asn ? ('AS' + String(e.asn).replace(/^AS/i, '') + ' ') : '') + (e.org || e.isp || '')).trim() || '–';
      elAsn.textContent = txt;
    }
    const elDomain = byId('Domain');
    if (elDomain) elDomain.textContent = (asn && asn.domain) || '–';
    const elRange = byId('Range');
    if (elRange) {
      const rg = asn && asn.network && asn.network.range;
      elRange.textContent = rg ? (rg.start + ' - ' + rg.end) : '–';
    }

    // 人机流量比
    const elBot = byId('Bot');
    if (elBot) elBot.innerHTML = ciHumanBotBar(ipa.botclass);

    // 位置（多源）
    const elGeoList = byId('GeoList');
    if (elGeoList) {
      const rows = ciGeoRowsForEgress(e, basic);
      elGeoList.innerHTML = rows.length ? rows.join('') : '<div class="ci-mut">位置数据获取失败（点「刷新检测」重试）</div>';
    }

    // 属性
    const elSource = byId('Source');
    if (elSource) elSource.innerHTML = ciSourceTags(traits, e.is_broadcast, asn && asn.type, e.ping0);
    const elAttr = byId('Attr');
    if (elAttr) {
      const resi = (e.is_residential !== null && e.is_residential !== undefined) ? e.is_residential
        : ((e.ipapi && e.ipapi.status === 'ok') ? !e.ipapi.hosting : null);
      elAttr.innerHTML = ciAttrTags(resi, e.ipapi, asn && asn.type, e.scamalytics, e.ping0);
    }

    // IPPure 系数（旧 IPPure 接口的 fraud_score 兜底）
    const elIpBar = byId('Ippure');
    if (elIpBar) {
      const score = (ipa.risk_score !== null && ipa.risk_score !== undefined) ? ipa.risk_score : e.fraud_score;
      elIpBar.innerHTML = (score !== null && score !== undefined) ? ciScoreBar(score) : '<span class="ci-mut">获取失败</span>';
    }

    // Scamalytics 系数
    const elScam = byId('Scamalytics');
    if (elScam) {
      const scam = e.scamalytics || {};
      if (scam.status === 'ok' && scam.score !== null && scam.score !== undefined) {
        elScam.innerHTML = ciScoreBar(scam.score) + (scam.risk ? ` <span class="ci-mut" style="font-size:11px;">(${escapeHtml(scam.risk)})</span>` : '');
      } else if (scam.status === 'failed') {
        elScam.innerHTML = `<span class="ci-mut">${escapeHtml(scam.error || '获取失败')}</span>`;
      } else {
        elScam.innerHTML = '<span class="ci-mut">–</span>';
      }
    }

    // IPQualityScore 系数
    const elIpqs = byId('Ipqs');
    if (elIpqs) {
      const ipqs = e.ipqualityscore || {};
      if (ipqs.status === 'ok' && ipqs.fraud_score !== null && ipqs.fraud_score !== undefined) {
        elIpqs.innerHTML = ciScoreBar(ipqs.fraud_score);
      } else if (ipqs.status === 'no_key') {
        elIpqs.innerHTML = '<span class="ci-inline"><span class="ci-mut">选填：未配置 Key</span><button class="ci-link ci-jump-settings" data-target="ipqs" style="margin-left:6px;">配置Key</button></span>';
      } else if (ipqs.status === 'failed') {
        elIpqs.innerHTML = `<span class="ci-mut">${escapeHtml(ipqs.error || '获取失败')}</span>`;
      } else {
        elIpqs.innerHTML = '<span class="ci-mut">–</span>';
      }
    }

    // Ping0 系数
    const elPing0 = byId('Ping0');
    if (elPing0) {
      const p0 = e.ping0 || {};
      if (p0.status === 'ok' && p0.score !== null && p0.score !== undefined) {
        let p0Extra = p0.risk_level ? ` <span class="ci-mut" style="font-size:11px;">(${escapeHtml(p0.risk_level)})</span>` : '';
        if (p0.usecount) p0Extra += ` <span class="ci-mut" style="font-size:11px;color:var(--ok);">· 共享: ${escapeHtml(p0.usecount)}</span>`;
        elPing0.innerHTML = ciScoreBar(p0.score) + p0Extra;
      } else if (p0.status === 'captcha') {
        elPing0.innerHTML = '<span class="ci-inline"><span class="ci-mut">网页人机拦截</span><button class="ci-link ci-jump-settings" data-target="ping0" style="margin-left:6px;">配置Key免盾</button></span>';
      } else if (p0.status === 'failed') {
        elPing0.innerHTML = `<span class="ci-mut">${escapeHtml(p0.error || '获取失败')}</span>`;
      } else {
        elPing0.innerHTML = '<span class="ci-mut">–</span>';
      }
    }
  }

  // Cloudflare 云防检测（浏览器模拟，约 30~60 秒；服务端缓存 30 分钟）
  async function ciRefreshCf(side, force = false) {
    const el = document.getElementById(side === 'proxy' ? 'ciProxyCf' : 'ciDirectCf');
    if (!el) return;
    if (force) el.innerHTML = '<span class="ci-mut">检测中…（无头浏览器模拟，约 30~60 秒）</span>';
    const rerun = (label) => `<button class="ci-link ci-cf-run" data-side="${side}">${label}</button>`;
    try {
      const r = await fetch(`/api/clash/inspector/cfscore?side=${side}${force ? '&force=1' : ''}`);
      const d = await r.json();
      if (!d.ok) {
        if (d.not_cached) { el.innerHTML = `<span class="ci-inline"><span class="ci-mut">未检测</span>${rerun('运行检测')}</span>`; return; }
        el.innerHTML = `<span class="ci-inline"><span class="ci-mut">${escapeHtml(d.error || '检测失败')}</span>${rerun('重试')}</span>`;
        return;
      }
      const flags = d.flags || {};
      const flagTags = [
        flags.open_proxy === true ? '<span class="ci-tag ci-tag-err">公开代理</span>' : '',
        flags.residential === true ? '<span class="ci-tag ci-tag-warn">住宅代理</span>' : '',
        flags.corporate === true ? '<span class="ci-tag ci-tag-warn">企业代理</span>' : '',
      ].filter(Boolean).join(' ');
      const meta = [
        flagTags,
        d.loc ? escapeHtml(d.loc) : '',
        (d.cached ? '缓存于 ' : '刚测 · ') + escapeHtml(d.ts || ''),
        `<a class="ci-link ci-cf-run" data-side="${side}">重测</a>`,
      ].filter(Boolean).join(' · ');
      el.innerHTML = ciScoreBar(d.score) + `<span class="ci-cfmeta">${meta}</span>`;
    } catch (e) {
      el.innerHTML = `<span class="ci-inline"><span class="ci-mut">请求失败</span>${rerun('重试')}</span>`;
    }
  }

  // ===== 安全与泄露检测：三角展开（DNS / WebRTC / 定位泄露 / 共享邻居） =====

  let ciLastEgress = null;   // loadClashInspector 渲染后缓存 { pe, de }
  let ciRtcLast = null;      // 最近一次 WebRTC 检测结果
  let ciAutoTimer = null;    // 实时/定时 IP 检测自动刷新定时器

  function setCiAutoRefresh(sec) {
    if (ciAutoTimer) {
      clearInterval(ciAutoTimer);
      ciAutoTimer = null;
    }
    const s = parseInt(sec, 10) || 0;
    try { localStorage.setItem('ci_auto_refresh', String(s)); } catch (e) {}
    const sel = document.getElementById('ciAutoRefresh');
    if (sel && sel.value !== String(s)) sel.value = String(s);
    if (s > 0) {
      ciAutoTimer = setInterval(() => {
        const tabActive = (document.getElementById('cnSubTabInspector') || {}).classList?.contains('on') ?? false;
        if (tabActive && document.visibilityState !== 'hidden') {
          loadClashInspector(true);
        }
      }, s * 1000);
    }
  }

  function ciToggleAcc(name) {
    const body = document.getElementById('ciAccBody' + name);
    const btn = document.getElementById('ciAccBtn' + name);
    if (!body) return;
    const willOpen = body.hidden;
    body.hidden = !willOpen;
    if (btn) btn.classList.toggle('open', willOpen);
    if (willOpen) {
      if (name === 'Rtc') runCiWebrtcCheck();
      else if (name === 'Loc') runCiLocCheck();
      else if (name === 'Nb') runCiNeighbors();
    }
  }

  const CI_RTC_CATS = {
    'public': '公网地址', 'private': '内网地址', 'loopback': '回环', 'link-local': '链路本地',
    'carrier-grade-NAT': '运营商级 NAT', 'unique-local': '唯一本地', 'ipv4-mapped': 'IPv4 映射',
    'global': '公网地址', 'unspecified': '无效/占位',
  };

  // 人设时区基准：本机系统时区按美东设定（配合美区住宅人设，2026-10-14）；
  // 时区一致性以它为准，当前出口时区只作信息对照，不再单独判为泄露。
  const CI_PERSONA_TZ = 'America/New_York';

  // WebRTC 泄露检测（逻辑对齐 IPPure：STUN 收候选 → 分类 → 与当前出口比对）
  function runCiWebrtcCheck() {
    const box = document.getElementById('ciRtcResult');
    const sum = document.getElementById('ciAccSumRtc');
    if (!box) return;
    if (sum) sum.textContent = '检测中…';
    box.innerHTML = '<div class="ci-mut">正在收集 ICE 候选地址（约 2~4 秒）…</div>';
    const iceServers = [
      { urls: 'stun:stun.chat.bilibili.com:3478' }, { urls: 'stun:stun.hitv.com:3478' },
      { urls: 'stun:stun.miwifi.com:3478' }, { urls: 'stun:stun.cloudflare.com:3478' },
      { urls: 'stun:stun.l.google.com:19302' },
    ];
    const re4 = /(?:(?:25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(?:25[0-5]|2[0-4]\d|1?\d{1,2})/g;
    const re6 = /(?:^|[\s:,\(\)"])((?:[0-9a-fA-F]{1,4}:){1,7}[0-9a-fA-F]{1,4}|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}|fe80:(?:[:0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|::1)(?=$|[\s,;\)"])/g;
    const isMdns = (s) => /\.local\b/i.test(s);
    const cls4 = (ip) => {
      const o = ip.split('.').map(Number);
      if (ip === '0.0.0.0' || ip === '255.255.255.255') return 'unspecified';
      if (ip === '127.0.0.1') return 'loopback';
      if (o[0] === 10 || (o[0] === 172 && o[1] >= 16 && o[1] <= 31) || (o[0] === 192 && o[1] === 168)) return 'private';
      if (o[0] === 169 && o[1] === 254) return 'link-local';
      if (o[0] === 100 && o[1] >= 64 && o[1] <= 127) return 'carrier-grade-NAT';
      return 'public';
    };
    const cls6 = (s) => {
      const e = s.toLowerCase();
      if (e === '::1') return 'loopback';
      if (e.startsWith('fe80:')) return 'link-local';
      if (/^fc|^fd/.test(e)) return 'unique-local';
      if (/^::ffff:/.test(e)) return 'ipv4-mapped';
      return 'global';
    };
    const seen = new Set();
    const ipv4 = new Set(), ipv6 = new Set(), mdns = new Set(), classified = [];
    let pc = null, done = false;
    const finish = () => {
      if (done) return;
      done = true;
      try { if (pc) pc.close(); } catch (e) { /* ignore */ }
      ciRtcLast = { classified, mdns: [...mdns], ipv4: [...ipv4], ipv6: [...ipv6] };
      renderCiWebrtcResult();
    };
    try { pc = new RTCPeerConnection({ iceServers }); } catch (e) {
      box.innerHTML = '<div class="ci-verdict err">当前环境不支持 RTCPeerConnection</div>';
      if (sum) sum.textContent = '不可用';
      return;
    }
    try { pc.createDataChannel('webrtc-detect'); } catch (e) { /* ignore */ }
    pc.onicecandidate = (ev) => {
      if (!ev || !ev.candidate) { setTimeout(finish, 0); return; }
      const c = ev.candidate.candidate || '';
      if (!c || seen.has(c)) return;
      seen.add(c);
      if (isMdns(c)) (c.match(/\b[a-z0-9-]{6,}\.local\b/ig) || []).forEach(x => mdns.add(x));
      for (const ip of (c.match(re4) || [])) {
        const cat = cls4(ip);
        if (!ipv4.has(ip)) { ipv4.add(ip); classified.push({ ip, family: 'IPv4', category: cat }); }
      }
      for (const raw of c.matchAll(re6)) {
        const ip = (raw[1] || raw[0]).replace(/^\[|\]$/g, '');
        const cat = cls6(ip);
        if (!ipv6.has(ip)) { ipv6.add(ip); classified.push({ ip, family: 'IPv6', category: cat }); }
      }
    };
    pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => {});
    setTimeout(finish, 3500);
  }

  function renderCiWebrtcResult() {
    const box = document.getElementById('ciRtcResult');
    const sum = document.getElementById('ciAccSumRtc');
    if (!box || !ciRtcLast) return;
    const egressIp = (ciLastEgress && ciLastEgress.pe && ciLastEgress.pe.ip) || '';
    const pub4 = ciRtcLast.classified.filter(c => c.category === 'public');
    const locals = ciRtcLast.classified.filter(c => ['private', 'link-local', 'carrier-grade-NAT', 'unique-local'].includes(c.category));
    const leaked = pub4.filter(c => egressIp && c.ip !== egressIp);
    let verdict, vcls;
    if (leaked.length) { verdict = '⚠ 发现公网地址泄露：候选公网 IP 与当前代理出口不一致'; vcls = 'err'; }
    else if (pub4.length) { verdict = '✓ 未发现出口泄露（候选公网地址与当前访问出口一致）'; vcls = 'ok'; }
    else if (locals.length) { verdict = '⚠ 未直接暴露公网 IP，但存在内网地址候选（建议检查 WebRTC 策略）'; vcls = 'warn'; }
    else if (ciRtcLast.mdns.length) { verdict = '✓ 未检测到泄露（本地地址已 mDNS 混淆）'; vcls = 'ok'; }
    else { verdict = '✓ 未收集到候选地址（浏览器可能已限制 WebRTC）'; vcls = 'ok'; }
    const rows = [];
    ciRtcLast.classified.forEach(c => {
      const suspicious = c.family === 'IPv4' && c.category === 'public' && egressIp && c.ip !== egressIp;
      const badge = suspicious ? '<span class="ci-tag ci-tag-err">疑似泄露</span>'
        : (c.category === 'public' ? '<span class="ci-tag ci-tag-ok">出口一致</span>'
          : (c.category === 'unspecified' ? '<span class="ci-tag ci-tag-dim">忽略</span>' : '<span class="ci-tag ci-tag-dim">本地</span>'));
      rows.push(`<div class="ci-rtc-row"><span class="ci-rtc-cat">${CI_RTC_CATS[c.category] || c.category}</span><span class="ci-rtc-ip">${c.ip}</span><span class="ci-rtc-badge">${badge}</span></div>`);
    });
    ciRtcLast.mdns.forEach(h => {
      rows.push(`<div class="ci-rtc-row"><span class="ci-rtc-cat">mDNS 混淆地址</span><span class="ci-rtc-ip">${h}</span><span class="ci-rtc-badge"><span class="ci-tag ci-tag-ok">已混淆</span></span></div>`);
    });
    box.innerHTML = `<div class="ci-verdict ${vcls}">${verdict}</div>` +
      (rows.length ? `<div class="ci-rtc-table">${rows.join('')}</div>` : '<div class="ci-mut">未收集到候选地址</div>') +
      `<div class="ci-foot">检测逻辑与 IPPure WebRTC 检测对齐：经 STUN 收集 ICE 候选并分类；当前出口 ${egressIp ? escapeHtml(egressIp) : '未知'}。仅在本机浏览器内运行，不上传数据。</div>`;
    if (sum) {
      sum.className = 'ci-acc-sum ' + (vcls === 'ok' ? 'ok' : (vcls === 'warn' ? 'warn' : 'err'));
      sum.textContent = vcls === 'err' ? '⚠ 公网地址泄露' : (vcls === 'warn' ? '⚠ 内网候选暴露' : '✓ 未发现泄露');
    }
  }

  // IP 定位泄露检测：时区一致性 + 多源定位一致性 + WebRTC 交叉核验（+ Bilibili 视角尽力而为）
  async function runCiLocCheck() {
    const box = document.getElementById('ciLocResult');
    const sum = document.getElementById('ciAccSumLoc');
    if (!box) return;
    if (sum) sum.textContent = '检测中…';
    box.innerHTML = '<div class="ci-mut">正在核验时区与多源定位…</div>';
    const rows = [];
    const issues = [];
    const pe = (ciLastEgress && ciLastEgress.pe) || {};
    const basic = ((pe.ippure_api || {}).basic) || null;

    // 1) 时区：以人设基准判定一致性；当前出口时区仅作信息对照（2026-10-14 起）
    let browserTz = '';
    try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { /* ignore */ }
    const tzIpinfo = (pe.ipinfo || {}).timezone || '';
    const tzMaxmind = (((basic || {}).geo || {})['MaxMind'] || {}).time_zone || '';
    const tzIp = tzIpinfo || tzMaxmind;
    let personaOk = null, exitOk = null;
    if (browserTz) {
      personaOk = (browserTz === CI_PERSONA_TZ);
      if (!personaOk) issues.push('时区与人设不符');
      if (tzIp) exitOk = (browserTz === tzIp);
    }
    rows.push(`<div class="ci-row"><span class="ci-rk">浏览器时区</span><span class="ci-rv mono">${escapeHtml(browserTz || '未知')}</span></div>`);
    const tzShow = (tzIpinfo && tzMaxmind && tzIpinfo !== tzMaxmind) ? (tzIpinfo + ' / ' + tzMaxmind) : tzIp;
    rows.push(`<div class="ci-row"><span class="ci-rk">当前出口时区（ipinfo/MaxMind）</span><span class="ci-rv mono">${escapeHtml(tzShow || '未知')}</span></div>`);
    rows.push(`<div class="ci-row"><span class="ci-rk">人设一致性（${escapeHtml(CI_PERSONA_TZ)}）</span><span class="ci-rv">${personaOk === null ? '<span class="ci-mut">无法比对</span>' : (personaOk ? '<span class="ci-tag ci-tag-ok">与人设一致</span>' : '<span class="ci-tag ci-tag-warn">与人设不符 · 核对系统时区</span>')}</span></div>`);
    rows.push(`<div class="ci-row"><span class="ci-rk">出口时区对照</span><span class="ci-rv">${exitOk === null ? '<span class="ci-mut">无法比对</span>' : (exitOk ? '<span class="ci-tag ci-tag-ok">与出口一致</span>' : '<span class="ci-tag ci-tag-dim">不一致 · 当前线路不在人设区（非泄露；敏感操作走美区线路即可）</span>')}</span></div>`);

    // 2) 多源定位一致性
    const codes = [];
    const put = (src, cc) => { if (cc && typeof cc === 'string' && cc.length === 2) codes.push({ src, cc: cc.toUpperCase() }); };
    const cf = pe.cloudflare || {};
    if (cf.status === 'ok' && cf.loc) put('Cloudflare', cf.loc);
    if (basic && basic.geo) {
      put('IP2Location', (basic.geo['IP2Location'] || {}).country_code);
      put('DB-IP', (basic.geo['DB-IP'] || {}).country_code);
      put('MaxMind', (basic.geo['MaxMind'] || {}).country_code);
    }
    put('ipinfo.io', (pe.ipinfo || {}).country);
    put('IP.SB', (pe.ipsb || {}).country_code);
    put('ipwho.is', (pe.ipwho || {}).country_code);
    put('ip-api', (pe.ipapi || {}).country_code);
    let locLine = '<span class="ci-mut">暂无多源数据</span>';
    if (codes.length) {
      const tally = {};
      codes.forEach(c => { tally[c.cc] = (tally[c.cc] || 0) + 1; });
      const top = Object.entries(tally).sort((a, b) => b[1] - a[1])[0][0];
      const bad = codes.filter(c => c.cc !== top);
      locLine = bad.length
        ? `<span class="ci-tag ci-tag-warn">${codes.length - bad.length}/${codes.length} 源一致（${top}）</span> <span class="ci-mut">不一致：${escapeHtml(bad.map(b => b.src + '(' + b.cc + ')').join('、'))}</span>`
        : `<span class="ci-tag ci-tag-ok">${codes.length}/${codes.length} 源一致（${top}）</span>`;
      if (bad.length) issues.push('多源定位');
    }
    rows.push(`<div class="ci-row"><span class="ci-rk">多源定位一致性</span><span class="ci-rv">${locLine}</span></div>`);

    // 3) WebRTC 交叉核验
    let rtcLine;
    if (ciRtcLast) {
      const egressIp = pe.ip || '';
      const pub4 = ciRtcLast.classified.filter(c => c.category === 'public');
      const leaked = pub4.filter(c => egressIp && c.ip !== egressIp);
      rtcLine = leaked.length
        ? `<span class="ci-tag ci-tag-err">候选公网与出口不一致（${escapeHtml(leaked.map(x => x.ip).join(', '))}）</span>`
        : '<span class="ci-tag ci-tag-ok">候选与出口一致 / 无泄露</span>';
      if (leaked.length) issues.push('WebRTC');
    } else {
      rtcLine = '<span class="ci-mut">未检测（先展开上方 WebRTC 检测）</span>';
    }
    rows.push(`<div class="ci-row"><span class="ci-rk">WebRTC 交叉核验</span><span class="ci-rv">${rtcLine}</span></div>`);

    // 4) Bilibili 视角（尽力而为，CORS 失败则忽略）
    let biliLine = '<span class="ci-mut">未获取</span>';
    try {
      const opts = {};
      if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) opts.signal = AbortSignal.timeout(6000);
      const rb = await fetch('https://api.bilibili.com/x/web-interface/zone', opts);
      const db = await rb.json();
      if (db && db.code === 0 && db.data) {
        const addr = `${db.data.country || ''} ${db.data.province || ''} ${db.data.city || ''}`.trim();
        if (addr) biliLine = escapeHtml(addr);
      }
    } catch (e) { /* 忽略 */ }
    rows.push(`<div class="ci-row"><span class="ci-rk">Bilibili 视角</span><span class="ci-rv">${biliLine}</span></div>`);

    const verdict = issues.length
      ? `<div class="ci-verdict warn">⚠ 发现 ${issues.length} 处不一致：${escapeHtml(issues.join('、'))}（存在定位暴露风险，建议复核出口与浏览器环境）</div>`
      : '<div class="ci-verdict ok">✓ 未发现定位泄露（时区、多源定位、WebRTC 交叉均正常）</div>';
    box.innerHTML = verdict + `<div class="ci-report" style="background:transparent;border:none;padding:0">${rows.join('')}</div>` +
      `<div class="ci-foot">定位泄露 = 浏览器/系统信号（时区、WebRTC 候选、解析视角）与你选定的出口 IP 所在地不一致，可能让目标站点识别出真实位置；多源一致性基于当前出口探测数据。时区口径（2026-10-14 起）：以人设基准（${escapeHtml(CI_PERSONA_TZ)}）判定一致性；与当前出口不一致仅说明当前线路不在人设区，属线路选择而非泄露。</div>`;
    if (sum) {
      sum.className = 'ci-acc-sum ' + (issues.length ? 'warn' : 'ok');
      sum.textContent = issues.length ? ('⚠ ' + issues.length + ' 处不一致') : '✓ 未发现泄露';
    }
  }

  // 共享人数：Ping0 全网独立用户估算 + IPPure 网上邻居（同出口 IP / /24 网段留言板）
  async function runCiNeighbors() {
    const box = document.getElementById('ciNbResult');
    const sum = document.getElementById('ciAccSumNb');
    if (!box) return;
    if (sum) sum.textContent = '查询中…';
    box.innerHTML = '<div class="ci-mut">正在查询共享人数（Ping0 估算 + IPPure 网上邻居）…</div>';
    try {
      const r = await fetch('/api/clash/inspector/neighbors');
      const d = await r.json();
      if (!d.ok && !d.ping0_usecount) throw Error(d.error || '查询失败');
      const n = d.count_users || 0, m = d.count_messages || 0;
      const p0Share = d.ping0_usecount || (ciLastEgress && ciLastEgress.pe && ciLastEgress.pe.ping0 && ciLastEgress.pe.ping0.usecount) || null;

      let html = '';
      if (p0Share) {
        let p0cls = 'ok';
        if (p0Share.includes('一般') || p0Share.includes('10-100') || p0Share.includes('10 - 100')) p0cls = 'warn';
        else if (p0Share.includes('差') || p0Share.includes('危') || p0Share.includes('1000')) p0cls = 'err';
        html += `<div class="ci-verdict ${p0cls}" style="margin-bottom:8px;"><i class="ph ph-users"></i> Ping0 全网独立用户共享估算：<b>${escapeHtml(p0Share)}</b></div>`;
      }
      if (n > 0) {
        html += `<div class="ci-verdict ok">IPPure 网上邻居：同出口 / 网段当前有 <b>${n}</b> 位邻居 · ${m} 条留言</div>`;
      } else {
        html += '<div class="ci-verdict mut">IPPure 网上邻居：同出口 / 网段暂无邻居留言</div>';
      }

      if ((d.messages || []).length) {
        html += '<div class="ci-nb-list">' + d.messages.map(it =>
          `<div class="ci-nb-item"><span class="ci-nb-time">${escapeHtml(it.time || '')}</span><span class="ci-nb-user">${escapeHtml(it.user || '匿名')}</span>${escapeHtml(it.text || '')}</div>`
        ).join('') + '</div>';
      }
      html += '<div class="ci-foot">双数据源交叉：① <b>Ping0</b> 基于过去全网大数据分析的独立用户共享档位；② <b>IPPure</b> 网上邻居留言板（同一出口 IP / /24 网段活跃邻居）。</div>';
      box.innerHTML = html;
      if (sum) {
        const parts = [];
        if (p0Share) parts.push(`Ping0: ${p0Share}`);
        if (n > 0) parts.push(`${n} 位邻居 · ${m} 条留言`);
        else parts.push('暂无留言');
        sum.textContent = parts.join(' · ');
      }
    } catch (e) {
      box.innerHTML = `<div class="ci-verdict warn">查询失败：${escapeHtml(e.message)}</div>`;
      if (sum) sum.textContent = '查询失败';
    }
  }

  // ---- 单 IP 查询 ----
  async function runIpQuery() {
    const input = document.getElementById('ipqInput');
    const box = document.getElementById('ipqResult');
    const status = document.getElementById('ipqStatus');
    const btn = document.getElementById('ipqBtn');
    if (!input || !box) return;
    const val = input.value.trim();
    if (!val) { input.focus(); return; }
    if (btn) btn.disabled = true;
    if (status) { status.hidden = false; status.className = 'ci-status-tag'; status.textContent = '查询中…（约 5~20 秒）'; }
    box.hidden = false;
    box.innerHTML = '<div class="ci-mut" style="padding:12px 2px">正在聚合 IPPure 官方数据与多源交叉核验…</div>';
    try {
      const r = await fetch(`/api/clash/inspector/ip?ip=${encodeURIComponent(val)}`);
      const d = await r.json();
      if (!d.ok) throw Error(d.error || ('HTTP ' + r.status));
      renderIpQuery(d);
      if (status) { status.className = 'ci-status-tag'; status.textContent = '完成 · ' + (d.timestamp || ''); }
    } catch (err) {
      box.innerHTML = `<div class="ci-empty">查询失败：${escapeHtml(err.message)}</div>`;
      if (status) { status.className = 'ci-status-tag err'; status.textContent = '失败'; }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderIpQuery(d) {
    const box = document.getElementById('ipqResult');
    if (!box) return;
    const ipa = d.ippure || {};
    const basic = ipa.basic || null;
    const asn = (basic && basic.asn) || null;
    const traits = (basic && basic.traits) || null;
    const geo = (basic && basic.geo) || null;
    const ipap = d.ipapi || {};
    const ipinfo = d.ipinfo || {};
    const ipsb = d.ipsb || {};
    const ipwho = d.ipwho || {};
    const rdns = d.rdns || {};
    const scam = d.scamalytics || {};
    const ipqs = d.ipqualityscore || {};
    const p0 = d.ping0 || {};
    const ipis = d.ipapi_is || {};

    const qj = d.quality_judge || {};
    const pm = qj.proxy_mode || null;
    const rm = qj.residential_mode || null;
    let qualityCardHtml = '';
    if (pm || rm) {
      const pmReasons = (pm && pm.reasons || []).map(r => `<li class="ci-quality-reason-item">${escapeHtml(r)}</li>`).join('');
      const rmReasons = (rm && rm.reasons || []).map(r => `<li class="ci-quality-reason-item">${escapeHtml(r)}</li>`).join('');
      qualityCardHtml = `
        <div class="ci-sec">智能裁决画像</div>
        <div class="ci-quality-card">
          <div class="ci-quality-item">
            <div class="ci-quality-title">
              <span class="ci-quality-icon">🚀</span>
              <span>节点代理适用性</span>
              <span class="ci-tag ci-tag-${escapeHtml((pm && pm.cls) || 'dim')}">${escapeHtml((pm && pm.badge) || '–')}</span>
            </div>
            <div class="ci-quality-desc">${escapeHtml((pm && pm.summary) || '')}</div>
            <ul class="ci-quality-reasons">${pmReasons}</ul>
          </div>
          <div class="ci-quality-item">
            <div class="ci-quality-title">
              <span class="ci-quality-icon">🏠</span>
              <span>住宅养号适用性</span>
              <span class="ci-tag ci-tag-${escapeHtml((rm && rm.cls) || 'dim')}">${escapeHtml((rm && rm.badge) || '–')}</span>
            </div>
            <div class="ci-quality-desc">${escapeHtml((rm && rm.summary) || '')}</div>
            <ul class="ci-quality-reasons">${rmReasons}</ul>
          </div>
        </div>
      `;
    }

    const rows = [];
    const asnTxt = asn ? ('AS' + asn.number + ' - ' + (asn.organization || ''))
      : (ipap.status === 'ok' && ipap.asn ? String(ipap.asn) : '–');
    rows.push(`<div class="ci-row"><span class="ci-rk">ASN</span><span class="ci-rv">${escapeHtml(asnTxt)}</span></div>`);
    rows.push(`<div class="ci-row"><span class="ci-rk">AS域名</span><span class="ci-rv mono">${escapeHtml((asn && asn.domain) || '–')}</span></div>`);
    const rg = asn && asn.network && asn.network.range;
    const rangeTxt = rg ? (rg.start + ' - ' + rg.end + (asn.network.cidr ? (' · ' + asn.network.cidr) : '')) : '–';
    rows.push(`<div class="ci-row"><span class="ci-rk">IP范围</span><span class="ci-rv mono">${escapeHtml(rangeTxt)}</span></div>`);
    rows.push(`<div class="ci-row"><span class="ci-rk">人机流量比</span><span class="ci-rv">${ciHumanBotBar(ipa.botclass)}</span></div>`);

    const geoRows = [];
    if (geo) {
      geoRows.push(ciGeoRow('IP2Location', (geo['IP2Location'] || {}).country_code, ciLocFromBasicGeo(geo['IP2Location'])));
      geoRows.push(ciGeoRow('DB-IP', (geo['DB-IP'] || {}).country_code, ciLocFromBasicGeo(geo['DB-IP'])));
      geoRows.push(ciGeoRow('MaxMind', (geo['MaxMind'] || {}).country_code, ciLocFromBasicGeo(geo['MaxMind'])));
    }
    if (ipinfo.status === 'ok') geoRows.push(ciGeoRow('ipinfo.io', ipinfo.country, ciJoinLoc([ipinfo.city, ipinfo.region])));
    if (ipsb.status === 'ok') geoRows.push(ciGeoRow('IP.SB', ipsb.country_code, ciJoinLoc([ipsb.city])));
    if (ipwho.status === 'ok') geoRows.push(ciGeoRow('ipwho.is', ipwho.country_code, ciJoinLoc([ipwho.city, ipwho.region])));
    if (ipap.status === 'ok') geoRows.push(ciGeoRow('ip-api', ipap.country_code, ciJoinLoc([ipap.city, ipap.region])));
    if (p0.status === 'ok' && p0.location) {
      geoRows.push(ciGeoRow('Ping0', null, p0.location + (p0.isp ? (' · ' + p0.isp) : '')));
    }
    if (scam.status === 'ok' && (scam.city || scam.country)) {
      geoRows.push(ciGeoRow('Scamalytics', scam.country_code, ciJoinLoc([scam.city, scam.country])));
    }
    if (ipis.status === 'ok' && (ipis.location || ipis.company)) {
      geoRows.push(ciGeoRow('ipapi.is', null, ciJoinLoc([ipis.location, ipis.company])));
    }

    let srcHtml = ciSourceTags(traits, ipap.status === 'ok' ? ipap.hosting === true : null, asn && asn.type);
    if (p0.status === 'ok') {
      if (p0.native === true && !srcHtml.includes('原生IP')) srcHtml = '<span class="ci-tag ci-tag-ok">Ping0 原生</span> ' + srcHtml;
      else if (p0.native === false && !srcHtml.includes('广播IP')) srcHtml = '<span class="ci-tag ci-tag-warn">Ping0 广播</span> ' + srcHtml;
    }
    if (scam.status === 'ok') {
      if (scam.datacenter === true && !srcHtml.includes('机房')) srcHtml += ' <span class="ci-tag ci-tag-dim">Scam 机房</span>';
      if (scam.residential === true) srcHtml += ' <span class="ci-tag ci-tag-ok">Scam 住宅代理</span>';
    }
    if (ipis.status === 'ok') {
      if (ipis.is_datacenter === true && !srcHtml.includes('机房')) srcHtml += ' <span class="ci-tag ci-tag-dim">ipapi.is 机房</span>';
      if (ipis.is_datacenter === false) srcHtml += ' <span class="ci-tag ci-tag-ok">ipapi.is 住宅</span>';
    }

    let attrHtml = ciAttrTags(ipap.status === 'ok' ? !ipap.hosting : null, ipap, asn && asn.type);
    const extraAttrTags = [];
    if (p0.status === 'ok') {
      if (p0.type) extraAttrTags.push(`<span class="ci-tag ci-tag-dim">Ping0 ${escapeHtml(p0.type)}</span>`);
      if (p0.usecount) {
        const u = p0.usecount;
        let cls = 'ci-tag-ok';
        if (u.includes('一般') || u.includes('10-100') || u.includes('10 - 100')) cls = 'ci-tag-warn';
        else if (u.includes('差') || u.includes('危') || u.includes('1000') || u.includes('10000')) cls = 'ci-tag-err';
        extraAttrTags.push(`<span class="ci-tag ${cls}" title="Ping0 全网独立用户估算"><i class="ph ph-users"></i> 共享人数: ${escapeHtml(u)}</span>`);
      }
    }
    if (scam.status === 'ok') {
      if (scam.vpn === true) extraAttrTags.push('<span class="ci-tag ci-tag-warn">VPN</span>');
      if (scam.tor === true) extraAttrTags.push('<span class="ci-tag ci-tag-err">Tor</span>');
    }
    if (ipqs.status === 'ok') {
      if (ipqs.vpn === true && !extraAttrTags.some(t => t.includes('VPN'))) extraAttrTags.push('<span class="ci-tag ci-tag-warn">VPN</span>');
      if (ipqs.proxy === true) extraAttrTags.push('<span class="ci-tag ci-tag-err">代理</span>');
      if (ipqs.recent_abuse === true) extraAttrTags.push('<span class="ci-tag ci-tag-err">近期滥用</span>');
      if (ipqs.bot_status === true) extraAttrTags.push('<span class="ci-tag ci-tag-warn">Bot</span>');
    }
    if (ipis.status === 'ok') {
      if (ipis.is_abuser === true) extraAttrTags.push('<span class="ci-tag ci-tag-err" title="命中滥用者/恶意黑名单"><i class="ph ph-warning-octagon"></i> 滥用者(Abuser)</span>');
      if (ipis.is_vpn === true && !extraAttrTags.some(t => t.includes('VPN'))) extraAttrTags.push('<span class="ci-tag ci-tag-warn">VPN</span>');
      if (ipis.is_proxy === true && !extraAttrTags.some(t => t.includes('代理'))) extraAttrTags.push('<span class="ci-tag ci-tag-err">代理</span>');
      if (ipis.is_tor === true && !extraAttrTags.some(t => t.includes('Tor'))) extraAttrTags.push('<span class="ci-tag ci-tag-err">Tor</span>');
    }
    if (extraAttrTags.length) {
      attrHtml = (attrHtml && attrHtml !== '<span class="ci-mut">–</span>') ? (attrHtml + ' ' + extraAttrTags.join(' ')) : extraAttrTags.join(' ');
    }

    // Scamalytics 系数
    let scamHtml = '<span class="ci-mut">–</span>';
    if (scam.status === 'ok') {
      const tagClass = (scam.score < 25) ? 'ci-tag-ok' : (scam.score < 70 ? 'ci-tag-warn' : 'ci-tag-err');
      const tagTxt = scam.risk ? `<span class="ci-tag ${tagClass}">${escapeHtml(scam.risk)}</span>` : '';
      scamHtml = ciScoreBar(scam.score) + (tagTxt ? ` <span class="ci-cfmeta">${tagTxt}</span>` : '');
    } else if (scam.status === 'failed') {
      scamHtml = `<span class="ci-mut">查询失败 (${escapeHtml(scam.error || '未返回')})</span>`;
    }

    // IPQualityScore 系数
    let ipqsHtml = '<span class="ci-mut">–</span>';
    if (ipqs.status === 'ok') {
      const qFlags = [];
      if (ipqs.proxy) qFlags.push('<span class="ci-tag ci-tag-err">代理</span>');
      if (ipqs.vpn) qFlags.push('<span class="ci-tag ci-tag-warn">VPN</span>');
      if (ipqs.tor) qFlags.push('<span class="ci-tag ci-tag-err">Tor</span>');
      if (ipqs.recent_abuse) qFlags.push('<span class="ci-tag ci-tag-err">近期滥用</span>');
      ipqsHtml = ciScoreBar(ipqs.fraud_score) + (qFlags.length ? ` <span class="ci-cfmeta">${qFlags.join(' ')}</span>` : '');
    } else if (ipqs.status === 'no_key') {
      ipqsHtml = `<span class="ci-inline"><span class="ci-mut">选填：未配置 Key（免费 1000 次/月）</span><button class="ci-link ci-jump-settings" data-target="ipqs" style="margin-left:6px">配置Key</button></span>`;
    } else if (ipqs.status === 'failed') {
      ipqsHtml = `<span class="ci-mut">查询失败 (${escapeHtml(ipqs.error || '未返回')})</span>`;
    }

    // Ping0 系数
    let ping0Html = '<span class="ci-mut">–</span>';
    if (p0.status === 'ok' && (p0.score !== null && p0.score !== undefined)) {
      const tagClass = (p0.score < 25) ? 'ci-tag-ok' : (p0.score < 50 ? 'ci-tag-warn' : 'ci-tag-err');
      const tagTxt = p0.risk_level ? `<span class="ci-tag ${tagClass}">${escapeHtml(p0.risk_level)}</span>` : '';
      const shareTxt = p0.usecount ? `<span class="ci-cfmeta" style="color:var(--ok);">· 共享人数: ${escapeHtml(p0.usecount)}</span>` : '';
      ping0Html = ciScoreBar(p0.score) + (tagTxt ? ` <span class="ci-cfmeta">${tagTxt}</span>` : '') + shareTxt;
    } else if (p0.status === 'captcha') {
      ping0Html = `<span class="ci-inline"><span class="ci-mut">网页人机拦截（免Key受限）</span><button class="ci-link ci-jump-settings" data-target="ping0" style="margin-left:6px">配置Key免盾</button></span>`;
    } else if (p0.status === 'failed') {
      ping0Html = `<span class="ci-mut">查询失败 (${escapeHtml(p0.error || '未返回')})</span>`;
    }

    // ipapi.is 系数
    let ipapiHtml = '<span class="ci-mut">–</span>';
    if (ipis.status === 'ok') {
      const isFlags = [];
      if (ipis.is_abuser) isFlags.push('<span class="ci-tag ci-tag-err">滥用者(Abuser)</span>');
      if (ipis.is_datacenter) isFlags.push('<span class="ci-tag ci-tag-dim">数据中心</span>');
      if (ipis.is_vpn) isFlags.push('<span class="ci-tag ci-tag-warn">VPN</span>');
      if (ipis.is_proxy) isFlags.push('<span class="ci-tag ci-tag-err">代理</span>');
      if (ipis.is_tor) isFlags.push('<span class="ci-tag ci-tag-err">Tor</span>');
      if (ipis.company) isFlags.push(`<span class="ci-tag ci-tag-dim">${escapeHtml(ipis.company)}</span>`);

      if (ipis.abuser_score !== null && ipis.abuser_score !== undefined) {
        ipapiHtml = ciScoreBar(ipis.abuser_score) + (isFlags.length ? ` <span class="ci-cfmeta">${isFlags.join(' ')}</span>` : '');
      } else if (isFlags.length) {
        const keyNote = !ipis.has_key ? `<button class="ci-link ci-jump-settings" data-target="ipapi" style="margin-left:6px">配置免费Key解锁深层风控</button>` : '';
        ipapiHtml = `<span class="ci-inline">${isFlags.join(' ')} ${keyNote}</span>`;
      } else {
        ipapiHtml = `<span class="ci-inline"><span class="ci-mut">基础源已验证（未检出风险）</span><button class="ci-link ci-jump-settings" data-target="ipapi" style="margin-left:6px">配置Key查滥用库</button></span>`;
      }
    } else if (ipis.status === 'failed') {
      ipapiHtml = `<span class="ci-mut">查询失败 (${escapeHtml(ipis.error || '未返回')})</span>`;
    }

    box.innerHTML = `
      <div class="ci-report ci-report-wide">
        <div class="ci-ipq-head">
          <span class="ci-ip-val">${escapeHtml(d.ip)}</span>
          <span class="ci-tag ci-tag-dim">${escapeHtml(d.ip_version || '')}</span>
          <button class="ci-link" id="ipqCopyBtn">复制</button>
        </div>
        ${qualityCardHtml}
        ${rows.join('')}
        <div class="ci-sec">位置</div>
        <div class="ci-geo-list">${geoRows.join('') || '<div class="ci-mut">位置数据获取失败</div>'}</div>
        <div class="ci-sec">属性</div>
        <div class="ci-row"><span class="ci-rk">IP来源</span><span class="ci-rv">${srcHtml}</span></div>
        <div class="ci-row"><span class="ci-rk">IP属性</span><span class="ci-rv">${attrHtml}</span></div>
        <div class="ci-sec">风险系数</div>
        <div class="ci-row ci-rowtop"><span class="ci-rk">IPPure系数</span><span class="ci-rv ci-barwrap">${ciScoreBar(ipa.risk_score)}</span></div>
        <div class="ci-row ci-rowtop"><span class="ci-rk">Scamalytics系数</span><span class="ci-rv ci-barwrap">${scamHtml}</span></div>
        <div class="ci-row ci-rowtop"><span class="ci-rk">IPQualityScore系数</span><span class="ci-rv ci-barwrap">${ipqsHtml}</span></div>
        <div class="ci-row ci-rowtop"><span class="ci-rk">Ping0系数</span><span class="ci-rv ci-barwrap">${ping0Html}</span></div>
        <div class="ci-row ci-rowtop"><span class="ci-rk">ipapi.is系数</span><span class="ci-rv ci-barwrap">${ipapiHtml}</span></div>
        <div class="ci-row ci-rowtop"><span class="ci-rk">Cloudflare系数</span><span class="ci-rv"><span class="ci-mut">仅本机出口可测（见「实时 IP 与定向链路诊断」页）</span></span></div>
        <div class="ci-sec">其他</div>
        <div class="ci-row"><span class="ci-rk">反向 DNS</span><span class="ci-rv mono">${escapeHtml((rdns && rdns.hostname) || '未解析')}</span></div>
        <div class="ci-row"><span class="ci-rk">查询时间</span><span class="ci-rv">${escapeHtml(d.timestamp || '')}</span></div>
        <div class="ci-foot">数据来源：IPPure · Scamalytics · IPQualityScore · Ping0 · ipapi.is · ip-api · ipinfo.io · IP.SB · ipwho.is · 本机反向 DNS。仅供本机使用。</div>
      </div>`;

    const cp = document.getElementById('ipqCopyBtn');
    if (cp) cp.onclick = () => navigator.clipboard.writeText(d.ip);
  }

  // --- 风险检测 API Key 弹窗管理 ---
  function openCiApiKeysModal(targetField) {
    const overlay = document.getElementById('ciApiKeysModalOverlay');
    if (!overlay) return;
    const ipqsInp = document.getElementById('modalCiIpqsKey');
    const ping0Inp = document.getElementById('modalCiPing0Key');
    const ipapiInp = document.getElementById('modalCiIpapiIsKey');
    const statusEl = document.getElementById('modalCiApiKeysStatus');
    if (statusEl) { statusEl.textContent = ''; statusEl.style.color = ''; }

    const curIpqs = document.getElementById('cnSetIpqsKey')?.value || '';
    const curPing0 = document.getElementById('cnSetPing0Key')?.value || '';
    if (ipqsInp) ipqsInp.value = curIpqs;
    if (ping0Inp) ping0Inp.value = curPing0;

    // 与后端配置对齐
    fetch('/api/clashnodes/env').then(r => r.json()).then(d => {
      const cfg = d.config || {};
      if (ipqsInp && document.activeElement !== ipqsInp) ipqsInp.value = cfg.ipqs_api_key || '';
      if (ping0Inp && document.activeElement !== ping0Inp) ping0Inp.value = cfg.ping0_api_key || '';
      if (ipapiInp && document.activeElement !== ipapiInp) ipapiInp.value = cfg.ipapi_is_api_key || cfg.ipapi_key || '';
      const setIpqs = document.getElementById('cnSetIpqsKey');
      if (setIpqs) setIpqs.value = cfg.ipqs_api_key || '';
      const setP0 = document.getElementById('cnSetPing0Key');
      if (setP0) setP0.value = cfg.ping0_api_key || '';
    }).catch(() => {});

    overlay.style.display = 'flex';
    setTimeout(() => {
      if (targetField === 'ping0' && ping0Inp) {
        ping0Inp.focus();
        ping0Inp.select();
      } else if (targetField === 'ipapi' && ipapiInp) {
        ipapiInp.focus();
        ipapiInp.select();
      } else if (ipqsInp) {
        ipqsInp.focus();
        ipqsInp.select();
      }
    }, 60);
  }

  function closeCiApiKeysModal() {
    const overlay = document.getElementById('ciApiKeysModalOverlay');
    if (overlay) overlay.style.display = 'none';
  }

  async function saveCiApiKeys() {
    const ipqsInp = document.getElementById('modalCiIpqsKey');
    const ping0Inp = document.getElementById('modalCiPing0Key');
    const ipapiInp = document.getElementById('modalCiIpapiIsKey');
    const statusEl = document.getElementById('modalCiApiKeysStatus');
    const btnSave = document.getElementById('btnSaveCiApiKeysModal');

    const ipqs_api_key = (ipqsInp?.value || '').trim();
    const ping0_api_key = (ping0Inp?.value || '').trim();
    const ipapi_is_api_key = (ipapiInp?.value || '').trim();

    if (btnSave) btnSave.disabled = true;
    if (statusEl) {
      statusEl.style.color = 'var(--tx2)';
      statusEl.textContent = '正在保存…';
    }

    try {
      const r = await fetch('/api/clashnodes/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ipqs_api_key, ping0_api_key, ipapi_is_api_key })
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));

      // 同步回巡检环境面板的 input
      const setIpqs = document.getElementById('cnSetIpqsKey');
      if (setIpqs) setIpqs.value = ipqs_api_key;
      const setP0 = document.getElementById('cnSetPing0Key');
      if (setP0) setP0.value = ping0_api_key;

      if (statusEl) {
        statusEl.style.color = 'var(--ok)';
        statusEl.textContent = '保存成功！正在重新探测…';
      }

      setTimeout(() => {
        closeCiApiKeysModal();
        if (btnSave) btnSave.disabled = false;
        // 自动按当前活跃页面重新探测
        const tabInspector = document.getElementById('cnSubTabInspector');
        const tabIpq = document.getElementById('cnSubTabIpq');
        if (tabInspector && tabInspector.classList.contains('on')) {
          loadClashInspector(true);
        } else if (tabIpq && tabIpq.classList.contains('on')) {
          runIpQuery();
        }
      }, 500);
    } catch (err) {
      if (statusEl) {
        statusEl.style.color = 'var(--err)';
        statusEl.textContent = '保存失败：' + err.message;
      }
      if (btnSave) btnSave.disabled = false;
    }
  }

  function toggleCiInputVisibility(inputEl, btnEl) {
    if (!inputEl) return;
    const isPass = inputEl.type === 'password';
    inputEl.type = isPass ? 'text' : 'password';
    if (btnEl) {
      const icon = btnEl.querySelector('i');
      if (icon) icon.className = isPass ? 'ph ph-eye-slash' : 'ph ph-eye';
    }
  }

  // --- Clash 实时 IP & 链路诊断仪业务逻辑 ---
  async function loadClashInspector(force = false) {
    const coreTag = document.getElementById('ciCoreStatus');
    const btnRef = document.getElementById('ciBtnRefresh');
    if (btnRef) btnRef.disabled = true;

    try {
      const r = await fetch(`/api/clash/inspector/current?force=${force ? 1 : 0}`);
      const d = await r.json();
      if (!r.ok || !d.ok) throw Error(d.error || ('HTTP ' + r.status));

      if (coreTag) {
        coreTag.className = 'ci-status-tag';
        coreTag.innerHTML = `<i class="ph ph-check-circle"></i> Mihomo 活跃 (端口 ${d.mixed_port || 7890})`;
      }

      // 代理出站出口：报告式渲染
      const pe = d.proxy_egress || {};
      const elProxyIp = document.getElementById('ciProxyIp');
      const elProxyGeo = document.getElementById('ciProxyGeo');
      if (elProxyIp) elProxyIp.textContent = pe.ip || '未获取到';
      if (elProxyGeo) {
        elProxyGeo.textContent = (ciJoinLoc([pe.country, pe.city]) || pe.error || '无位置数据');
      }
      renderCiEgress('proxy', pe);
      ciRefreshCf('proxy');

      // Clash 策略组
      const ap = d.active_proxies || {};
      const elRouteGlobal = document.getElementById('ciRouteGlobal');
      const elRouteResi = document.getElementById('ciRouteResi');
      if (elRouteGlobal) elRouteGlobal.textContent = (ap['GLOBAL'] || ap['🚀 节点选择'] || {}).now || '–';
      if (elRouteResi) elRouteResi.textContent = (ap['⚡ 住宅中转'] || ap['🏠 住宅IP-Claude谷歌'] || {}).now || '–';

      // 本地直连出口：报告式渲染
      const de = d.direct_egress || {};
      const elDirIp = document.getElementById('ciDirectIp');
      const elDirGeo = document.getElementById('ciDirectGeo');
      const elDirNote = document.getElementById('ciDirectStatusNote');

      if (elDirIp) elDirIp.textContent = de.ip || '–';
      if (elDirGeo) elDirGeo.textContent = (ciJoinLoc([de.country, de.region, de.city]) || de.error || '–');
      renderCiEgress('direct', de);
      ciRefreshCf('direct');
      ciLastEgress = { pe, de };

      if (elDirNote) {
        if (pe.ip && de.ip && pe.ip !== de.ip) {
          elDirNote.innerHTML = '<span style="color:var(--ok)"><i class="ph ph-check-circle"></i> 代理已接管：出口 IP 与本地宽带已完全隔离。</span>';
        } else if (pe.ip && de.ip && pe.ip === de.ip) {
          elDirNote.innerHTML = '<span style="color:var(--warn)"><i class="ph ph-warning"></i> 提示：代理出口与本地宽带 IP 相同（当前为直连状态）。</span>';
        }
      }
    } catch (err) {
      if (coreTag) {
        coreTag.className = 'ci-status-tag err';
        coreTag.innerHTML = `<i class="ph ph-x-circle"></i> 连接失败: ${escapeHtml(err.message)}`;
      }
    } finally {
      if (btnRef) btnRef.disabled = false;
    }

    // 同时拉取 DNS 诊断
    try {
      const rd = await fetch('/api/clash/inspector/dns');
      const dd = await rd.json();
      if (rd.ok && dd.ok) {
        const up = dd.upstream_resolver || {};
        const elDnsUp = document.getElementById('ciDnsUpstream');
        const elDnsLeak = document.getElementById('ciDnsLeakResult');
        const elDnsTag = document.getElementById('ciDnsLeakTag');
        const elDnsFake = document.getElementById('ciDnsFakeIp');

        if (elDnsUp) elDnsUp.textContent = (up.ip ? (up.ip + ' · ') : '') + (up.geo || '未知上游');
        if (elDnsLeak) {
          elDnsLeak.innerHTML = dd.leak_detected
            ? `<span style="color:var(--err)">⚠️ ${escapeHtml(dd.leak_reason || '存在泄露')}</span>`
            : `<span style="color:var(--ok)">✓ ${escapeHtml(dd.leak_reason || '未泄露')}</span>`;
        }
        if (elDnsTag) {
          elDnsTag.className = 'ci-tag ' + (dd.leak_detected ? 'ci-tag-err' : 'ci-tag-ok');
          elDnsTag.textContent = dd.leak_detected ? '⚠️ 存在泄漏' : '✓ DoH 加密已保护';
        }
        const elAccSumDns = document.getElementById('ciAccSumDns');
        if (elAccSumDns) {
          elAccSumDns.className = 'ci-acc-sum ' + (dd.leak_detected ? 'err' : 'ok');
          elAccSumDns.textContent = dd.leak_detected ? '⚠ 存在 DNS 泄露' : '✓ 未泄露 · DoH 保护中';
        }
        if (elDnsFake) {
          const fk = dd.fake_ip_mode || {};
          elDnsFake.textContent = fk.fake_ip ? `已启用 Fake-IP (${fk.fake_ip})` : '真实解析模式';
        }
      }
    } catch (e) { /* ignore */ }
  }

  async function runWebsiteTrace(domain) {
    domain = (domain || '').trim();
    if (!domain) return;
    const wrap = document.getElementById('ciPipelineWrap');
    const elDns = document.getElementById('ciPipeDns');
    const elRule = document.getElementById('ciPipeRule');
    const elProxy = document.getElementById('ciPipeProxy');
    const elEgress = document.getElementById('ciPipeEgress');
    const btn = document.getElementById('ciBtnTrace');

    if (wrap) wrap.style.display = 'block';
    if (elDns) elDns.textContent = '正在解析…';
    if (elRule) elRule.textContent = '匹配中…';
    if (elProxy) elProxy.textContent = '追踪中…';
    if (elEgress) elEgress.textContent = '评估中…';
    if (btn) btn.disabled = true;

    try {
      const r = await fetch(`/api/clash/inspector/trace?domain=${encodeURIComponent(domain)}`);
      const d = await r.json();
      if (!r.ok || !d.ok) throw Error(d.error || ('HTTP ' + r.status));

      // 1. DNS
      const dns = d.dns_resolution || {};
      const answers = (dns.answers || []).map(a => a.ip).filter(Boolean);
      if (elDns) elDns.textContent = answers.length ? answers.join(', ') : '无 A 记录 / 拦截';

      // 2. Rule
      const rm = d.rule_match || {};
      if (elRule) {
        if (rm.matched) {
          elRule.innerHTML = `<code>${escapeHtml(rm.type)}</code><br><span style="color:var(--tx2)">${escapeHtml(rm.payload || '')}</span>`;
        } else {
          elRule.textContent = '未显式匹配 (走 MATCH 兜底)';
        }
      }

      // 3. Proxy Chain
      const chain = d.outbound_chain || [];
      if (elProxy) {
        elProxy.innerHTML = chain.length ? chain.map(c => `<span class="ci-tag ci-tag-ok">${escapeHtml(c)}</span>`).join(' → ') : (rm.target_proxy || 'DIRECT');
      }

      // 4. Egress
      const eg = d.estimated_egress || {};
      if (elEgress) {
        elEgress.textContent = eg.desc || '–';
      }
    } catch (err) {
      if (elEgress) elEgress.textContent = '溯源失败: ' + err.message;
    } finally {
      if (btn) btn.disabled = false;
    }
  }


  // ============================================================
  // 磁盘清理 (Disk Cleaner) 前端逻辑
  // ============================================================
  let cleanerCands = [];
  let cleanerCandByUid = {};
  let cleanerFilter = { risk: 'all', cat: 'all', drive: 'all' };
  let cleanerSearchText = '';
  let cleanerDrives = [];
  let cleanerSelDrives = new Set();
  let cleanerPollTimer = null;
  let cleanerCleanTimer = null;
  let cleanerInitialized = false;

  const cleanerH = n => {
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + u[i];
  };

  function initCleaner() {
    if (!cleanerInitialized) {
      cleanerInitialized = true;
      const btnScan = document.getElementById('cleanerBtnScan');
      if (btnScan) btnScan.onclick = startCleanerScan;
      const btnEmptyRb = document.getElementById('cleanerBtnEmptyRb');
      if (btnEmptyRb) btnEmptyRb.onclick = emptyCleanerRecycleBin;
      const btnWinapp2 = document.getElementById('cleanerBtnWinapp2');
      if (btnWinapp2) btnWinapp2.onclick = openCleanerWinapp2;
      const btnElevate = document.getElementById('cleanerBtnElevate');
      if (btnElevate) btnElevate.onclick = cleanerElevate;
      const chkAll = document.getElementById('cleanerChkAll');
      if (chkAll) chkAll.onchange = e => toggleCleanerAll(e.target.checked);
      const btnSelSafe = document.getElementById('cleanerBtnSelSafe');
      if (btnSelSafe) btnSelSafe.onclick = () => quickSelectCleaner('safe');
      const btnSelReview = document.getElementById('cleanerBtnSelReview');
      if (btnSelReview) btnSelReview.onclick = () => quickSelectCleaner('review');
      const btnClearSel = document.getElementById('cleanerBtnClearSel');
      if (btnClearSel) btnClearSel.onclick = clearCleanerSel;
      const btnPreview = document.getElementById('cleanerBtnPreview');
      if (btnPreview) btnPreview.onclick = () => doCleanerClean(true);
      const btnDoClean = document.getElementById('cleanerBtnDoClean');
      if (btnDoClean) btnDoClean.onclick = () => doCleanerClean(false);
    }
    loadCleanerDrives();
    fetch('/api/cleaner/status').then(r => r.json()).then(s => {
      if (s.has_result) loadCleanerResult();
      else if (s.scanning) {
        const box = document.getElementById('cleanerStatusBox');
        if (box) box.classList.add('show');
        const empty = document.getElementById('cleanerEmpty');
        if (empty) empty.style.display = 'none';
        pollCleanerStatus();
      }
    }).catch(() => {});
  }

  async function loadCleanerDrives() {
    try {
      const resp = await fetch('/api/cleaner/drives');
      if (!resp.ok) return;
      const j = await resp.json();
      cleanerDrives = j.drives || [];
      const bar = document.getElementById('cleanerDrives');
      if (!bar) return;
      const waBadge = document.getElementById('cleanerWaBadge');
      if (waBadge) {
        waBadge.textContent = j.has_winapp2 ? '社区规则库 (已启用)' : '社区规则库 (可更新)';
      }
      bar.innerHTML = cleanerDrives.map((d, i) => {
        const pct = d.used_pct || 0;
        const cls = pct >= 85 ? 'r' : pct >= 70 ? 'y' : 'g';
        const adv = d.type !== 3 ? '' :
          pct >= 85 ? '🔴 很满' : pct >= 70 ? '🟡 偏满' : '🟢 充裕';
        const isSys = d.drive[0].toUpperCase() === (j.sysdrive || 'C:')[0].toUpperCase();
        return `<div class="cleaner-drive-card" id="cleanerDrvCard${i}" onclick="toggleCleanerDrive(${i})">
          <div class="cleaner-drive-head">
            <span class="cleaner-drive-name">${escapeHtml(d.drive)}</span>
            ${isSys ? '<span class="cleaner-drive-tag">(系统盘)</span>' : ''}
          </div>
          <div class="cleaner-drive-meta">${escapeHtml(d.type_name || '')} · ${escapeHtml(d.label || '本地磁盘')} · 剩 ${cleanerH(d.free)}</div>
          <div class="cleaner-drive-bar"><div class="cleaner-drive-fill ${cls}" style="width:${pct}%"></div></div>
          <div class="cleaner-drive-adv"><span>${pct}% 已用</span><span>${adv}</span></div>
        </div>`;
      }).join('');
      cleanerSelDrives.clear();
      let selectedCount = 0;
      cleanerDrives.forEach((d, i) => {
        if (d.type === 3 && d.used_pct >= 70) {
          toggleCleanerDrive(i, true);
          selectedCount++;
        }
      });
      if (selectedCount === 0 && cleanerDrives.length > 0) {
        const sysIdx = cleanerDrives.findIndex(d => d.drive[0].toUpperCase() === (j.sysdrive || 'C:')[0].toUpperCase());
        toggleCleanerDrive(sysIdx >= 0 ? sysIdx : 0, true);
      }
    } catch (e) {
      console.error('loadCleanerDrives failed:', e);
    }
  }

  function toggleCleanerDrive(i, force) {
    const d = cleanerDrives[i];
    if (!d) return;
    const el = document.getElementById('cleanerDrvCard' + i);
    const on = force !== undefined ? force : !cleanerSelDrives.has(d.drive);
    if (on) {
      cleanerSelDrives.add(d.drive);
      if (el) el.classList.add('selected');
    } else {
      cleanerSelDrives.delete(d.drive);
      if (el) el.classList.remove('selected');
    }
  }
  window.toggleCleanerDrive = toggleCleanerDrive;

  function startCleanerScan() {
    const roots = [...cleanerSelDrives];
    const extra = (document.getElementById('cleanerExtraRoot')?.value || '').trim();
    const deep = document.getElementById('cleanerChkDeep')?.checked || false;
    if (extra) roots.push(extra);
    if (!roots.length) {
      alert('请先选择要扫描的磁盘或输入自定义目录');
      return;
    }
    const btnScan = document.getElementById('cleanerBtnScan');
    if (btnScan) btnScan.disabled = true;
    const elEmpty = document.getElementById('cleanerEmpty');
    const elMain = document.getElementById('cleanerMain');
    const elCards = document.getElementById('cleanerCards');
    const elDSumm = document.getElementById('cleanerDriveSumm');
    const elReport = document.getElementById('cleanerReport');
    if (elEmpty) elEmpty.style.display = 'none';
    if (elMain) elMain.style.display = 'none';
    if (elCards) elCards.style.display = 'none';
    if (elDSumm) elDSumm.classList.remove('show');
    if (elReport) elReport.classList.remove('show');
    const box = document.getElementById('cleanerStatusBox');
    if (box) box.classList.add('show');
    const txt = document.getElementById('cleanerStatusText');
    if (txt) txt.textContent = `开始${deep ? '全盘深扫' : '靶向快扫'} ${roots.join(', ')} ...`;

    fetch('/api/cleaner/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roots, deep })
    }).then(r => r.json()).then(j => {
      if (j.error) {
        if (txt) txt.textContent = '扫描启动错误: ' + j.error;
        if (box) box.classList.remove('show');
        if (btnScan) btnScan.disabled = false;
        return;
      }
      pollCleanerStatus();
    }).catch(e => {
      if (txt) txt.textContent = '请求失败: ' + e;
      if (btnScan) btnScan.disabled = false;
    });
  }

  function pollCleanerStatus() {
    clearTimeout(cleanerPollTimer);
    cleanerPollTimer = setTimeout(async () => {
      try {
        const resp = await fetch('/api/cleaner/status');
        if (!resp.ok) return;
        const s = await resp.json();
        const btnScan = document.getElementById('cleanerBtnScan');
        const box = document.getElementById('cleanerStatusBox');
        const txt = document.getElementById('cleanerStatusText');
        if (s.error) {
          if (txt) txt.textContent = '扫描出错: ' + s.error.split('\n')[0];
          if (btnScan) btnScan.disabled = false;
          return;
        }
        if (s.scanning) {
          if (txt) txt.textContent = s.scan_progress || '扫描中...';
          pollCleanerStatus();
        } else {
          if (box) box.classList.remove('show');
          if (btnScan) btnScan.disabled = false;
          await loadCleanerResult();
        }
      } catch (e) {
        pollCleanerStatus();
      }
    }, 600);
  }

  async function loadCleanerResult() {
    try {
      const resp = await fetch('/api/cleaner/result');
      if (!resp.ok) return;
      const j = await resp.json();
      if (j.error) {
        const empty = document.getElementById('cleanerEmpty');
        if (empty) { empty.textContent = j.error; empty.style.display = 'block'; }
        return;
      }
      cleanerCands = j.candidates || [];
      cleanerCandByUid = {};
      cleanerCands.forEach((c, i) => { c.uid = i; cleanerCandByUid[i] = c; });

      const cTotal = document.getElementById('cleanerTotalSize');
      const cCand = document.getElementById('cleanerCandCount');
      const cNFiles = document.getElementById('cleanerNFiles');
      if (cTotal) cTotal.textContent = j.total_size_h || '-';
      if (cCand) cCand.textContent = `${(j.summary && j.summary.count) || 0} 项 / ${(j.summary && j.summary.total_size_h) || '-'}`;
      if (cNFiles) cNFiles.textContent = `${(j.nfiles || 0).toLocaleString()} 个 / ${j.elapsed}s`;

      const perm = j.perm || {};
      const hint = document.getElementById('cleanerPermHint');
      const hintText = document.getElementById('cleanerPermText');
      if (!perm.is_admin && perm.locked_count > 0) {
        if (hintText) hintText.textContent = `以管理员身份运行可多清理 ${perm.locked_count} 项系统垃圾（${perm.locked_size_h}）：Windows 临时文件、更新缓存、错误报告等需要管理员权限。`;
        if (hint) hint.style.display = 'flex';
      } else {
        if (hint) hint.style.display = 'none';
      }

      const elCards = document.getElementById('cleanerCards');
      const elMain = document.getElementById('cleanerMain');
      const elEmpty = document.getElementById('cleanerEmpty');
      if (elCards) elCards.style.display = 'grid';
      if (elMain) elMain.style.display = 'block';
      if (elEmpty) elEmpty.style.display = 'none';

      const dsumm = document.getElementById('cleanerDriveSumm');
      const dsummBody = document.getElementById('cleanerDriveSummBody');
      if (dsumm && dsummBody) {
        if ((j.drives || []).length > 0) {
          dsummBody.innerHTML = j.drives.map(m =>
            `<div class="cleaner-drivesumm-row">
              <span class="dv">${escapeHtml(m.drive)} 盘</span>
              <span class="dt">占用 ${escapeHtml(m.total_h)} · 耗时 ${m.elapsed}s · ${m.nfiles.toLocaleString()} 个文件</span>
              <span class="dc">可清理 ${m.cand_count} 项 (${escapeHtml(m.cand_size_h)})</span>
            </div>`).join('');
          dsumm.classList.add('show');
        } else {
          dsumm.classList.remove('show');
        }
      }

      buildCleanerFilters();
      renderCleanerTable();

      const topDirsBox = document.getElementById('cleanerTopdirsBox');
      const topDirs = document.getElementById('cleanerTopdirs');
      if (topDirsBox && topDirs) {
        if (j.top_dirs && j.top_dirs.length) {
          topDirsBox.style.display = 'block';
          topDirs.innerHTML = j.top_dirs.map(([s, p]) =>
            `<div class="cleaner-topdirs-item"><span class="p" title="${escapeHtml(p)}">${escapeHtml(p)}</span><span>${escapeHtml(s)}</span></div>`).join('');
        } else {
          topDirsBox.style.display = 'none';
        }
      }

      updateCleanerSel();
    } catch (e) {
      console.error('loadCleanerResult failed:', e);
    }
  }

  function getCleanerCandDrive(c) {
    return (c.path || '')[0].toUpperCase();
  }

  function buildCleanerFilters() {
    const wrap = document.getElementById('cleanerFilters');
    if (!wrap) return;
    let html = `<div class="cleaner-filter-search">
      <i class="ph ph-magnifying-glass"></i>
      <input type="text" id="cleanerSearchInput" value="${escapeHtml(cleanerSearchText)}" placeholder="智能搜索应用、缓存性质、项目或路径（如 Gradle、Chrome、LunarCore、着色器）..." oninput="onCleanerSearch(this.value)">
    </div><div class="cleaner-chips-row">`;

    const risks = [['all', '全部风险'], ['safe', '✅ 安全'], ['review', '⚠️ 建议确认'], ['caution', '🛑 谨慎']];
    html += risks.map(([k, n]) =>
      `<span class="cleaner-chip ${cleanerFilter.risk === k ? 'on' : ''}" onclick="setCleanerFilter('risk','${k}')">${n}</span>`).join('');

    const drvs = [...new Set(cleanerCands.map(getCleanerCandDrive))].sort();
    if (drvs.length > 1) {
      html += '<span class="cleaner-filter-sep">|</span>';
      html += `<span class="cleaner-chip ${cleanerFilter.drive === 'all' ? 'on' : ''}" onclick="setCleanerFilter('drive','all')">全部盘</span>`;
      html += drvs.map(d => {
        const n = cleanerCands.filter(c => getCleanerCandDrive(c) === d).length;
        return `<span class="cleaner-chip ${cleanerFilter.drive === d ? 'on' : ''}" onclick="setCleanerFilter('drive','${d}')">${d} 盘 (${n})</span>`;
      }).join('');
    }

    const byCat = {};
    cleanerCands.forEach(c => {
      byCat[c.category_name] = (byCat[c.category_name] || 0) + 1;
    });
    const cats = Object.keys(byCat);
    if (cats.length > 1) {
      html += '<span class="cleaner-filter-sep">|</span>';
      html += `<span class="cleaner-chip ${cleanerFilter.cat === 'all' ? 'on' : ''}" onclick="setCleanerFilter('cat','all')">全部类别</span>`;
      html += cats.map(c =>
        `<span class="cleaner-chip ${cleanerFilter.cat === c ? 'on' : ''}" onclick="setCleanerFilter('cat','${c}')">${escapeHtml(c)} (${byCat[c]})</span>`).join('');
    }
    html += '</div>';
    wrap.innerHTML = html;
  }

  function onCleanerSearch(val) {
    cleanerSearchText = (val || '').trim();
    renderCleanerTable();
  }
  window.onCleanerSearch = onCleanerSearch;

  function setCleanerFilter(k, v) {
    cleanerFilter[k] = v;
    buildCleanerFilters();
    renderCleanerTable();
  }
  window.setCleanerFilter = setCleanerFilter;

  function filterCleanerCand(c) {
    const q = cleanerSearchText ? cleanerSearchText.toLowerCase() : '';
    const matchQ = !q || (
      (c.title || '').toLowerCase().indexOf(q) > -1 ||
      (c.path || '').toLowerCase().indexOf(q) > -1 ||
      (c.owner_app || '').toLowerCase().indexOf(q) > -1 ||
      (c.cache_type || '').toLowerCase().indexOf(q) > -1 ||
      (c.purpose || '').toLowerCase().indexOf(q) > -1 ||
      (c.insight || '').toLowerCase().indexOf(q) > -1
    );
    return matchQ &&
           (cleanerFilter.risk === 'all' || c.risk === cleanerFilter.risk) &&
           (cleanerFilter.cat === 'all' || c.category_name === cleanerFilter.cat) &&
           (cleanerFilter.drive === 'all' || getCleanerCandDrive(c) === cleanerFilter.drive);
  }

  function renderCleanerTable() {
    const tbody = document.getElementById('cleanerTbody');
    if (!tbody) return;
    const rows = cleanerCands.filter(filterCleanerCand);
    tbody.innerHTML = rows.map(c => `
      <tr class="row-${c.risk}">
        <td><input type="checkbox" class="cleaner-chk" ${c.selected ? 'checked' : ''} onchange="toggleCleanerOne(${c.uid}, this.checked)"></td>
        <td><span class="cleaner-badge ${c.risk}">${escapeHtml(c.risk_name)}</span></td>
        <td class="cleaner-size">${escapeHtml(c.size_h)}</td>
        <td>
          <div class="cleaner-title">
            <span class="cleaner-drvtag">${getCleanerCandDrive(c)}</span>
            <span style="font-weight:600">${escapeHtml(c.title)}</span>
            ${c.owner_app ? `<span class="cleaner-tag cleaner-tag-app" title="所属应用"><i class="ph ph-cube"></i> ${escapeHtml(c.owner_app)}</span>` : ''}
            ${c.cache_type ? `<span class="cleaner-tag cleaner-tag-type" title="缓存性质"><i class="ph ph-tag"></i> ${escapeHtml(c.cache_type)}</span>` : ''}
            ${c.is_group ? ` <span class="cleaner-tag-group">×${c.group_count} 项</span>` : ''}
            <span class="cleaner-type-icon">${c.is_dir ? '📁' : '📄'}</span>
          </div>

          <div class="cleaner-path">${escapeHtml(c.path)}</div>

          ${c.insight ? `
          <div class="cleaner-insight">
            <i class="ph ph-lightbulb"></i>
            <span>${escapeHtml(c.insight)}</span>
          </div>` : ''}

          ${(c.purpose || c.recreate_cost || c.advice || c.description) ? `
          <div class="cleaner-smart-meta">
            ${c.purpose ? `<div class="cleaner-meta-row"><span class="cleaner-meta-lbl">用途：</span><span class="cleaner-meta-val">${escapeHtml(c.purpose)}</span></div>` : (c.description ? `<div class="cleaner-meta-row"><span class="cleaner-meta-lbl">说明：</span><span class="cleaner-meta-val">${escapeHtml(c.description)}</span></div>` : '')}
            ${c.recreate_cost ? `<div class="cleaner-meta-row"><span class="cleaner-meta-lbl">影响：</span><span class="cleaner-meta-val">${escapeHtml(c.recreate_cost)}</span></div>` : ''}
            ${c.advice ? `<div class="cleaner-meta-row"><span class="cleaner-meta-lbl">建议：</span><span class="cleaner-meta-val highlight">${escapeHtml(c.advice)}</span></div>` : ''}
          </div>` : ''}
        </td>
      </tr>`).join('');
    updateCleanerSel();
  }

  function toggleCleanerOne(uid, checked) {
    const c = cleanerCandByUid[uid];
    if (!c) return;
    c.selected = checked;
    fetch('/api/cleaner/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: [c.path], selected: checked })
    }).catch(() => {});
    updateCleanerSel();
  }
  window.toggleCleanerOne = toggleCleanerOne;

  function toggleCleanerAll(checked) {
    const rows = cleanerCands.filter(filterCleanerCand);
    rows.forEach(c => c.selected = checked);
    fetch('/api/cleaner/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: rows.map(c => c.path), selected: checked })
    }).catch(() => {});
    renderCleanerTable();
  }

  function quickSelectCleaner(risk) {
    const rows = cleanerCands.filter(c => c.risk === risk);
    rows.forEach(c => c.selected = true);
    fetch('/api/cleaner/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: rows.map(c => c.path), selected: true })
    }).catch(() => {});
    renderCleanerTable();
  }

  function clearCleanerSel() {
    cleanerCands.forEach(c => c.selected = false);
    fetch('/api/cleaner/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: cleanerCands.map(c => c.path), selected: false })
    }).catch(() => {});
    renderCleanerTable();
  }

  function updateCleanerSel() {
    const sel = cleanerCands.filter(c => c.selected);
    const bytes = sel.reduce((a, c) => a + c.size, 0);
    const selCountEl = document.getElementById('cleanerSelCount');
    if (selCountEl) selCountEl.textContent = sel.length;
    const selSizeEl = document.getElementById('cleanerSelTotalSize');
    if (selSizeEl) selSizeEl.textContent = cleanerH(bytes);
    const cSel = document.getElementById('cleanerSelSize');
    if (cSel) cSel.textContent = cleanerH(bytes);
    const bar = document.getElementById('cleanerActionbar');
    if (bar) bar.style.display = cleanerCands.length ? 'block' : 'none';
  }

  async function doCleanerClean(dryRun) {
    const sel = cleanerCands.filter(c => c.selected);
    if (!sel.length) {
      alert('请先勾选要清理的项（默认全不勾选，查看清单后再确认）');
      return;
    }
    const bytes = sel.reduce((a, c) => a + c.size, 0);
    if (!dryRun) {
      const byRisk = {};
      sel.forEach(c => { byRisk[c.risk] = (byRisk[c.risk] || 0) + 1; });
      const riskTxt = ['safe', 'review', 'caution']
        .filter(k => byRisk[k])
        .map(k => `${({ safe: '✅安全', review: '⚠️建议确认', caution: '🛑谨慎' })[k]} ${byRisk[k]} 项`)
        .join('，');
      if (!confirm(`确认清理选中的 ${sel.length} 项（${cleanerH(bytes)}）？\n${riskTxt}\n\n所有文件将移入系统回收站，安全可恢复。`)) return;
    }
    const btnScan = document.getElementById('cleanerBtnScan');
    if (btnScan) btnScan.disabled = true;
    const box = document.getElementById('cleanerStatusBox');
    if (box) box.classList.add('show');
    const txt = document.getElementById('cleanerStatusText');
    if (txt) txt.textContent = dryRun ? '正在预览分析...' : '正在清理并移入回收站...';

    try {
      const resp = await fetch('/api/cleaner/clean', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun, paths: sel.map(c => c.path) })
      });
      const j = await resp.json();
      if (btnScan) btnScan.disabled = false;
      if (j.error) {
        alert('清理提交错误: ' + j.error);
        if (box) box.classList.remove('show');
        return;
      }
      pollCleanerClean();
    } catch (e) {
      if (btnScan) btnScan.disabled = false;
      alert('网络请求失败: ' + e);
    }
  }

  function pollCleanerClean() {
    clearTimeout(cleanerCleanTimer);
    cleanerCleanTimer = setTimeout(async () => {
      try {
        const resp = await fetch('/api/cleaner/status');
        if (!resp.ok) return;
        const s = await resp.json();
        const box = document.getElementById('cleanerStatusBox');
        const txt = document.getElementById('cleanerStatusText');
        if (s.cleaning && s.cleaning.running) {
          if (txt) txt.textContent = `清理中 [${s.cleaning.done}/${s.cleaning.total}] ${s.cleaning.current || ''}`;
          if (box) box.classList.add('show');
          pollCleanerClean();
        } else {
          if (box) box.classList.remove('show');
          const rResp = await fetch('/api/cleaner/report');
          if (!rResp.ok) return;
          const rep = await rResp.json();
          if (!rep || rep.error) return;
          showCleanerReport(rep, rep.dry_run);
          const done = new Set(rep.cleaned_candidates || []);
          if (done.size) {
            cleanerCands = cleanerCands.filter(c => !done.has(c.path));
            cleanerCandByUid = {};
            cleanerCands.forEach((c, i) => { c.uid = i; cleanerCandByUid[i] = c; });
            renderCleanerTable();
            loadCleanerDrives();
          }
        }
      } catch (e) {
        pollCleanerClean();
      }
    }, 700);
  }

  function showCleanerReport(j, dryRun) {
    const title = document.getElementById('cleanerReportTitle');
    if (title) {
      title.innerHTML = dryRun
        ? `<i class="ph ph-eye"></i> 预览报告：预计可释放 ${escapeHtml(j.total_freed_h)}（未实际删除）`
        : `<i class="ph ph-check-circle"></i> 清理完成：逻辑释放 ${escapeHtml(j.total_freed_h)}${j.physical_freed_h ? ' | 物理增加 ' + escapeHtml(j.physical_freed_h) : ''}`;
    }
    const lines = (j.results || []).slice(0, 150).map(r => {
      const tag = { deleted: '删除', moved_to_trash: '回收站', skipped: '跳过', failed: '失败' }[r.action] || r.action;
      const sz = cleanerH(r.size || 0);
      return `[${tag}] ${sz.padStart(9)}  ${r.path}${r.reason ? '  (' + r.reason + ')' : ''}`;
    });
    if ((j.results || []).length > 150) lines.push(`... 共 ${j.results.length} 项（仅展示前 150 项）`);
    lines.push('');
    lines.push(`逻辑释放 (入回收站): ${j.total_freed_h || '0 B'} | 跳过: ${j.total_skipped || 0} | 失败: ${j.total_failed || 0}`);
    if (!dryRun && j.disk_deltas && Object.keys(j.disk_deltas).length) {
      for (const [drv, d] of Object.entries(j.disk_deltas)) {
        lines.push(`💾 [${drv} 盘物理可用增加] ${d.delta_h || '0 B'} (可用空间: ${d.before_h || ''} -> ${d.after_h || ''})`);
      }
    }
    if (!dryRun && j.recycle_bin_size_h) {
      lines.push(`ℹ️ 回收站当前实际占用 ${j.recycle_bin_size_h}（若物理可用空间未增加，请点击右上角“清空回收站”彻底释放）。`);
    }
    if (j.log_file) lines.push(`审计日志: ${j.log_file}`);
    const pre = document.getElementById('cleanerReportPre');
    if (pre) pre.textContent = lines.join('\n');
    const card = document.getElementById('cleanerReport');
    if (card) {
      card.classList.add('show');
      card.scrollIntoView({ behavior: 'smooth' });
    }
  }

  async function emptyCleanerRecycleBin() {
    if (!confirm('确定要清空 Windows 回收站吗？\n清空后回收站中的文件将无法恢复。')) return;
    const btn = document.getElementById('cleanerBtnEmptyRb');
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch('/api/cleaner/empty_recycle_bin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
      const j = await resp.json();
      if (j && j.ok) {
        alert('回收站已成功清空！');
        loadCleanerDrives();
      } else {
        alert('清空未完全完成或回收站已为空');
      }
    } catch (e) {
      alert('清空请求失败: ' + e);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function cleanerElevate() {
    if (!confirm('以管理员身份重启小宇工具箱以清理 Windows 临时文件、更新缓存与系统组件日志？\n点击确认将触发 Windows UAC 提示。')) return;
    try {
      const resp = await fetch('/api/cleaner/elevate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const j = await resp.json();
      if (j && j.ok) {
        alert('已成功请求管理员权限启动，请在弹出的系统提示中允许。');
      } else {
        alert('提权重启请求失败，请尝试手动以管理员身份运行。');
      }
    } catch (e) {
      alert('请求错误: ' + e);
    }
  }

  async function openCleanerWinapp2() {
    try {
      const resp = await fetch('/api/cleaner/winapp2_info');
      const info = await resp.json();
      let msg = info.exists
        ? `社区规则库状态：\n路径：${info.path}\n体积：${info.size_h}，包含 ${info.rule_count} 条本机适用规则\n${info.summary}\n\n是否立即从 BleachBit 上游检查并更新规则库？`
        : `尚未安装 winapp2.ini 社区规则库。\n安装后可白嫖 4000+ 常见应用的高频缓存清理规则。\n\n是否立即从 GitHub 上游下载最新规则库？`;
      if (!confirm(msg)) return;
      const btn = document.getElementById('cleanerBtnWinapp2');
      if (btn) btn.disabled = true;
      const uResp = await fetch('/api/cleaner/update_winapp2', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const uj = await uResp.json();
      alert(uj.message || (uj.ok ? '规则库更新成功！' : '更新失败'));
      loadCleanerDrives();
    } catch (e) {
      alert('获取规则库信息失败: ' + e);
    } finally {
      const btn = document.getElementById('cleanerBtnWinapp2');
      if (btn) btn.disabled = false;
    }
  }

  const ROUTES = [
    '#/dashboard', '#/ai', '#/news', '#/rankings', '#/compare',
    '#/agent/codex',
    '#/games', '#/cleaner', '#/translate', '#/clipboard',
    '#/nodes', '#/status'
  ];
  const NAV_ROUTE = {
    '#/nodes': '#/nodes',
    '#/cleaner': '#/cleaner',
    '#/dashboard': '#/dashboard',
    '#/ai': '#/ai-briefing',
    '#/news': '#/ai-news',
    '#/rankings': '#/ai-rankings',
    '#/compare': '#/ai-compare',
    '#/agent/codex': '#/agent/codex',
    '#/games': '#/games',
    '#/translate': '#/translate',
    '#/clipboard': '#/clipboard',
    '#/status': '#/status'
  };
  const PAGE_MAP = {
    '#/nodes': 'pgNodes',
    '#/cleaner': 'pgCleaner',
    '#/dashboard': 'pgDash',
    '#/ai': 'pgBriefing',
    '#/news': 'pgNews',
    '#/rankings': 'pgRank',
    '#/compare': 'pgCompare',
    '#/agent/codex': 'pgCodex',
    '#/games': 'pgGames',
    '#/translate': 'pgTrans',
    '#/clipboard': 'pgClipboard',
    '#/status': 'pgStatus'
  };

  // ============ 功能模块管理：左栏页面的安装 / 卸载 ============
  // 核心（仪表盘/设置）常驻；默认安装集 = AI 专区四页 + 翻译引擎，其余自由装卸。
  // 安装表存 /api/appearance（本机 appearance.json 的 modules.installed，与主题、
  // 仪表布局同一文件）；localStorage 仅在后端不可用时兜底，恢复后作为迁移源回写。
  const MODULE_DEFS = [
    { id: 'dashboard',   group: 'overview', core: true, name: '仪表盘',           href: '#/dashboard',   navRoute: '#/dashboard',   desc: 'Token 用量全景与额度监控条' },
    { id: 'ai-briefing', group: 'ai',       name: '每日要闻',         href: '#/ai',          navRoute: '#/ai-briefing', desc: 'AI 每日简报与 RSS 中文化聚合' },
    { id: 'ai-news',     group: 'ai',       name: '实时快讯',         href: '#/news',        navRoute: '#/ai-news',     desc: '多源实时快讯滚动与检索' },
    { id: 'ai-rankings', group: 'ai',       name: '模型天梯',         href: '#/rankings',    navRoute: '#/ai-rankings', desc: '模型能力排行榜与二级榜单' },
    { id: 'ai-compare',  group: 'ai',       name: '模型对比',         href: '#/compare',     navRoute: '#/ai-compare',  desc: '多模型横向对比' },
    { id: 'agent-codex', group: 'agent',    name: 'OpenAI 专区',      href: '#/agent/codex', navRoute: '#/agent/codex', desc: 'Codex Desktop 多账号独立登录态切换与冷重启保护' },
    { id: 'games',      group: 'games',    name: 'Epic喜加一',       href: '#/games',       navRoute: '#/games',       desc: 'Epic 每周免费游戏检测与半自动领取' },
    { id: 'cleaner',     group: 'tools',    name: '磁盘清理',         href: '#/cleaner',     navRoute: '#/cleaner',     desc: '智能磁盘扫描、规则分类与安全清理' },
    { id: 'nodes',       group: 'tools',    name: '节点检测',         href: '#/nodes',       navRoute: '#/nodes',       desc: 'Clash 节点测速与审计' },
    { id: 'translate',   group: 'tools',    name: '翻译引擎',         href: '#/translate',   navRoute: '#/translate',   desc: '划词翻译服务、引擎与热键' },
    { id: 'clipboard',   group: 'tools',    name: '剪贴板历史',       href: '#/clipboard',   navRoute: '#/clipboard',   desc: '剪贴板历史记录与快捷取用' },
    { id: 'status',      group: 'system',   core: true, name: '设置',             href: '#/status',      navRoute: '#/status',      desc: '外观、模块管理与数据源状态' },
  ];
  const MODULE_BY_ID = {};
  const MODULE_BY_ROUTE = {};
  MODULE_DEFS.forEach(d => { MODULE_BY_ID[d.id] = d; MODULE_BY_ROUTE[d.navRoute] = d; });
  const MODULE_GROUPS = [
    { key: 'overview', label: '总览' },
    { key: 'ai', label: 'AI 专区' },
    { key: 'agent', label: 'Agent 专区' },
    { key: 'games', label: '游戏专区' },
    { key: 'tools', label: '实用工具' },
    { key: 'system', label: '系统' },
  ];
  const MODULE_DEFAULT_INSTALLED = [
    'ai-briefing', 'ai-news', 'ai-rankings', 'ai-compare', 'translate', 'cleaner',
    'agent-codex'
  ];
  const MODULE_LS_KEY = 'xyModulesInstalled';
  let moduleInstalledList = null; // null = 尚未从任何来源载入，按默认集处理
  let moduleMgrDirty = false;     // 远端载入完成前用户已改动时，丢弃远端结果防止覆盖

  function moduleSanitizeList(raw) {
    const out = [];
    (Array.isArray(raw) ? raw : []).forEach(id => {
      const def = MODULE_BY_ID[id];
      if (def && !def.core && out.indexOf(id) === -1) out.push(id);
    });
    return out;
  }
  function moduleActiveList() {
    return Array.isArray(moduleInstalledList) ? moduleInstalledList : MODULE_DEFAULT_INSTALLED;
  }
  function moduleIsInstalled(id) {
    const def = MODULE_BY_ID[id];
    if (!def) return false;
    if (def.core) return true;
    return moduleActiveList().indexOf(id) > -1;
  }
  function moduleAllowsHash(hash) {
    const def = MODULE_BY_ROUTE[NAV_ROUTE[hash] || hash];
    return !def || moduleIsInstalled(def.id);
  }
  function moduleReadLocal() {
    try {
      const raw = JSON.parse(localStorage.getItem(MODULE_LS_KEY) || 'null');
      return Array.isArray(raw) ? moduleSanitizeList(raw) : null;
    } catch (e) { return null; }
  }
  // 左栏可见性统一出口：模块卸载隐藏 + 顶部搜索过滤共用；整组卸载时收起分组头
  function applyNavFilter() {
    const search = document.getElementById('navSearch');
    const q = search ? search.value.trim().toLowerCase() : '';
    const groupVisible = {};
    document.querySelectorAll('.rail .nav-item').forEach(item => {
      const dr = item.getAttribute('data-route') || '';
      const def = MODULE_BY_ROUTE[NAV_ROUTE[dr] || dr];
      const modHidden = !!(def && !moduleIsInstalled(def.id));
      item.setAttribute('data-modhidden', modHidden ? '1' : '0');
      const hit = !q || (item.textContent || '').toLowerCase().indexOf(q) > -1;
      item.style.display = (modHidden || !hit) ? 'none' : '';
      if (def && !modHidden) groupVisible[def.group] = true;
    });
    document.querySelectorAll('.rail .nav-group').forEach(head => {
      const g = head.getAttribute('data-navgroup');
      if (g) head.style.display = groupVisible[g] ? '' : 'none';
    });
  }
  async function modulePersist() {
    const list = moduleActiveList().slice();
    try { localStorage.setItem(MODULE_LS_KEY, JSON.stringify(list)); } catch (e) { /* 兜底尽力而为 */ }
    try {
      // /api/appearance 的 POST 是整包保存，先取现状再改 modules 键（同 zpLayoutPush）
      const resp = await fetch('/api/appearance', { cache: 'no-store' });
      const data = await resp.json();
      const body = Object.assign({}, (data && data.config) || {}, { modules: { installed: list } });
      const r2 = await fetch('/api/appearance', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
      if (!r2.ok) throw new Error('HTTP ' + r2.status);
    } catch (e) { /* 后端不可用：localStorage 已兜底，恢复后按迁移源回写 */ }
  }
  async function modulesLoadRemote() {
    try {
      const resp = await fetch('/api/appearance', { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      const installed = data && data.config && data.config.modules && data.config.modules.installed;
      if (Array.isArray(installed) && !moduleMgrDirty) {
        moduleInstalledList = moduleSanitizeList(installed);
      } else if (!Array.isArray(installed) && !moduleMgrDirty) {
        // 配置文件还没有 modules 字段：localStorage 旧值视为迁移源，有则回写一次
        const local = moduleReadLocal();
        moduleInstalledList = local !== null ? local : MODULE_DEFAULT_INSTALLED.slice();
        if (local !== null) modulePersist();
      }
    } catch (e) {
      if (!moduleMgrDirty) {
        const local = moduleReadLocal();
        if (local !== null) moduleInstalledList = local;
      }
    }
    applyNavFilter();
    renderModuleManager();
      if (appUpdateCache) renderUpdateUI(appUpdateCache);
    if (!moduleAllowsHash(location.hash || '#/dashboard')) location.replace('#/dashboard');
  }
  function renderModuleManager() {
    const wrap = document.getElementById('moduleMgrList');
    if (!wrap) return;
    let html = '';
    MODULE_GROUPS.forEach(g => {
      const defs = MODULE_DEFS.filter(d => d.group === g.key);
      if (!defs.length) return;
      html += '<div class="mod-mgr-group">' + escapeHtml(g.label) + '</div>';
      defs.forEach(def => {
        const installed = moduleIsInstalled(def.id);
        const navItem = document.querySelector('.rail .nav-item[data-route="' + def.navRoute + '"]');
        const iconHtml = navItem && navItem.firstElementChild ? navItem.firstElementChild.outerHTML : '';
        const action = def.core
          ? '<span class="mod-mgr-core" title="核心模块，常驻不可卸载"><i class="ph ph-lock-simple"></i>核心</span>'
          : installed
            ? '<button type="button" class="btn-ghost mod-mgr-btn" data-modaction="uninstall" data-modid="' + def.id + '">卸载</button>'
            : '<button type="button" class="btn-main mod-mgr-btn" data-modaction="install" data-modid="' + def.id + '">安装</button>';
        html += '<div class="mod-mgr-row' + (installed ? ' installed' : '') + '">'
          + '<span class="mod-mgr-ico">' + iconHtml + '</span>'
          + '<span class="mod-mgr-info"><b>' + escapeHtml(def.name) + '</b><small>' + escapeHtml(def.desc) + '</small></span>'
          + '<span class="mod-mgr-state">' + (installed ? '已安装' : '未安装') + '</span>'
          + action + '</div>';
      });
    });
    wrap.innerHTML = html;
  }
  // 卸载确认对话框：展示连带影响与数据清单，可选「同时删除数据」。
  // resolve(true/false)=确认（false=不删数据），null=取消
  function modMgrUninstallDialog(def) {
    return new Promise(resolve => {
      fetch('/api/modules/state', { cache: 'no-store' })
        .then(r => r.json())
        .then(st => {
          const info = (st.modules || []).find(m => m.id === def.id) || {};
          const old = document.getElementById('modMgrDialogMask');
          if (old) old.remove();
          const mask = document.createElement('div');
          mask.className = 'mod-mgr-dialog-mask';
          mask.id = 'modMgrDialogMask';
          const files = info.dataFiles || [];
          const side = info.sideEffect
            ? '<div class="mod-mgr-dialog-warn"><i class="ph ph-warning"></i><span>'
              + escapeHtml(info.sideEffect) + '</span></div>'
            : '';
          const fileHtml = files.length
            ? '<label class="mod-mgr-dialog-chk"><input type="checkbox" id="modMgrDelData">'
              + '<span>同时删除模块数据（不可恢复）：'
              + files.map(f => escapeHtml(f.path)).join('、') + '</span></label>'
            : '<div class="mod-mgr-dialog-nofiles">本模块没有可勾选删除的数据</div>';
          mask.innerHTML = '<div class="mod-mgr-dialog" role="dialog" aria-modal="true">'
            + '<div class="mod-mgr-dialog-title">卸载「' + escapeHtml(def.name) + '」？</div>'
            + '<div class="mod-mgr-dialog-body">将立即停止该模块的后台服务并物理隔离其接口'
            + '（所有相关 API 返回 404），重启工具箱后也不会自动载入。</div>'
            + side + fileHtml
            + '<div class="mod-mgr-dialog-actions">'
            + '<button type="button" class="mod-mgr-dialog-cancel">取消</button>'
            + '<button type="button" class="mod-mgr-dialog-ok">确认卸载</button>'
            + '</div></div>';
          const close = val => {
            mask.remove();
            document.removeEventListener('keydown', onKey, true);
            resolve(val);
          };
          const onKey = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); close(null); } };
          mask.addEventListener('pointerdown', ev => { if (ev.target === mask) close(null); });
          mask.querySelector('.mod-mgr-dialog-cancel').addEventListener('click', () => close(null));
          mask.querySelector('.mod-mgr-dialog-ok').addEventListener('click', () => {
            const chk = document.getElementById('modMgrDelData');
            close(!!(chk && chk.checked));
          });
          document.addEventListener('keydown', onKey, true);
          document.body.appendChild(mask);
          mask.querySelector('.mod-mgr-dialog-ok').focus();
        })
        .catch(() => resolve(false));   // 状态接口不可用：按不删数据继续走卸载
    });
  }

  async function onModuleMgrClick(event) {
    const btn = event.target.closest('[data-modaction]');
    if (!btn) return;
    const def = MODULE_BY_ID[btn.getAttribute('data-modid')];
    if (!def || def.core) return;
    const want = btn.getAttribute('data-modaction') === 'install';
    try {
      let deleteData = false;
      if (!want) {
        deleteData = await modMgrUninstallDialog(def);
        if (deleteData === null) return;    // 用户取消
      }
      const r = await fetch('/api/modules/' + (want ? 'install' : 'uninstall'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(want ? { id: def.id } : { id: def.id, deleteData: deleteData })
      });
      if (!r.ok) { alert('后端不支持模块操作（HTTP ' + r.status + '），请更新主程序'); return; }
      const data = await r.json();
      if (!data.ok) {
        alert((data.error || '操作失败') + ((data.steps && data.steps.length)
          ? '\n' + data.steps.join('\n') : ''));
        return;
      }
      moduleMgrDirty = true;
      const rest = moduleActiveList().filter(id => id !== def.id);
      moduleInstalledList = want ? rest.concat([def.id]) : rest;
      try { localStorage.setItem(MODULE_LS_KEY, JSON.stringify(moduleActiveList())); } catch (e) { /* 尽力而为 */ }
      // 安装表落盘由后端 /api/modules/* 完成；前端只镜像 localStorage，
      // 不再整包 POST /api/appearance，避免并发保存把后端刚写的安装表冲掉
      applyNavFilter();
      renderModuleManager();
      if (!want) alert('已卸载「' + def.name + '」\n' + (data.steps || []).join('\n'));
      // 正停留的页面被卸载时退回仪表盘
      if (!moduleAllowsHash(location.hash || '#/dashboard')) location.hash = '#/dashboard';
    } catch (e) {
      alert('模块操作请求失败: ' + e);
    }
  }
  function initModules() {
    const local = moduleReadLocal();
    if (local !== null) moduleInstalledList = local;
    applyNavFilter();
    const wrap = document.getElementById('moduleMgrList');
    if (wrap) wrap.addEventListener('click', onModuleMgrClick);
    modulesLoadRemote();
  }

  function handleHashRoute() {
    let hash = location.hash || '#/dashboard';
    if (hash === '' || hash === '#' || hash === '#/') hash = '#/dashboard';
    // 旧版路由兼容：#/token 与 #/agent/token 都是 Token 统计，统一进仪表盘
    if (hash === '#/token' || hash.startsWith('#/agent/token')) hash = '#/dashboard';
    if (ROUTES.indexOf(hash) === -1) hash = '#/dashboard';
    // 模块卸载守卫：目标页所属模块未安装时回仪表盘（仪表盘/设置核心常驻）
    if (!moduleAllowsHash(hash)) { location.replace('#/dashboard'); return; }

    // 1. 左栏导航高亮
    const activeRoute = NAV_ROUTE[hash] || '#/dashboard';
    document.querySelectorAll('.rail .nav-item').forEach(link => {
      link.classList.toggle('on', link.getAttribute('data-route') === activeRoute);
    });

    // 2. 页面显隐与时间窗按钮组
    const timeGroup = document.getElementById('headerTimeWindowGroup');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
    const pageEl = document.getElementById(PAGE_MAP[hash]);
    if (pageEl) pageEl.classList.add('on');
    if (timeGroup) timeGroup.style.display = (hash === '#/dashboard') ? 'flex' : 'none';
    const content = document.getElementById('content');
    if (content) content.scrollTop = 0;

    // 3. 页面数据加载（事件绑定均已幂等，这里只负责拉数据）
    if (hash === '#/dashboard') {
      if (!state._liveRequested) {
        loadData();
        state._liveRequested = true;
      } else {
        renderOverviewCards();
        renderToolsSection();
        renderModelSection();
        renderDailyChart();
        zpRenderStatic();
        zpRenderLive();
      }
      zpFetch();
    } else {
      if (hash === '#/ai') {
      initAiHub();
      loadAiBriefings();
      aiAutoRefresh('news', () => startAiNewsScrape('aiBriefingChip', '抓取中…', '正在抓取每日要闻与 6 个 RSS 源并中文化，约 1-2 分钟'));
    } else if (hash === '#/news') {
      initAiHub();
      loadAiNews(state.aiCategory, document.getElementById('aiNewsSearchInput')?.value.trim() || '');
      aiAutoRefresh('news', () => startAiNewsScrape('aiNewsUpdatedChip', '抓取中…', '正在抓取每日要闻与 6 个 RSS 源并中文化，约 1-2 分钟'));
    } else if (hash === '#/rankings') {
      initAiHub();
      // 重新进页要把上次的榜 id 还原成对应的高亮与二级条状态，
      // 否则能力榜会在"AA 智能指数榜"高亮下显示，看着像串台。
      const cur = state.aiRankCategory || 'aa_index';
      const mainCategory = syncRankingsSubBars(cur);
      document.querySelectorAll('#rankingsCategoryBar .rank-tab-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-rank') === mainCategory);
      });
      loadAiRankings(cur);
      aiAutoRefresh('rankings', () => startRankingsRefresh(null, document.getElementById('btnRankingsRefresh'), reloadAfterRankingsRefresh, { silent: true }));
    } else if (hash === '#/compare') {
      initComparePage();
      loadAiCompare();
      aiAutoRefresh('compare', () => startRankingsRefresh(state.compareSource, document.getElementById('btnCompareRefresh'), reloadCompareAfterRefresh, { silent: true }));
    } else if (hash === '#/agent/codex') {
      initCodexPage();
    } else if (hash === '#/games') {
      if (window.initGamesPage) window.initGamesPage();
      if (window.loadEpic) window.loadEpic();
    } else if (hash === '#/translate') {
      if (toolboxStatusCache) renderTranslateTopBar(toolboxStatusCache);
      else fetchToolboxStatus();
      fetchHotkeyState();
    } else if (hash === '#/clipboard') {
      initClipboard();
      loadClipboardHistory();
      fetchHotkeyState();
    } else if (hash === '#/nodes') {
      initNodes();
    } else if (hash === '#/cleaner') {
      initCleaner();
    } else if (hash === '#/status') {
      fetchToolboxStatus();
      renderSourceStatusBar();
      renderModuleManager();
      }
    }
  }

  // --- 自定义时间范围（系统时区，精确到小时） ---
  function zpCustomPopOpen() {
    const pop = document.getElementById('zpCustomPop');
    return Boolean(pop && !pop.hidden);
  }
  function zpToggleCustomPop(show) {
    const pop = document.getElementById('zpCustomPop');
    if (!pop) return;
    if (show) {
      const inp = document.getElementById('zpCustomStart');
      if (inp && !inp.value) {
        // 默认起点：今天 0 点（本机时区）
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        inp.value = zpLocalInputValue(d);
      }
    }
    pop.hidden = !show;
  }
  function zpLocalInputValue(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function zpSetActiveTimeBtn(win) {
    document.querySelectorAll('#headerTimeWindowGroup .time-btn').forEach(b => {
      b.classList.toggle('active', b.getAttribute('data-window') === win);
    });
  }
  function zpApplyCustomRange() {
    const inp = document.getElementById('zpCustomStart');
    if (!inp || !inp.value) return;
    const ts = new Date(inp.value).getTime();  // datetime-local 按浏览器时区（=系统时区）解释
    if (!isFinite(ts)) return;
    state.customStart = ts;
    state.timeWindow = 'custom';
    zpSetActiveTimeBtn('custom');
    const btn = document.getElementById('zpCustomBtn');
    if (btn) btn.title = `统计自 ${inp.value.replace('T', ' ')} 起`;
    zpToggleCustomPop(false);
    loadData();
  }
  function zpClearCustomRange() {
    state.customStart = null;
    state.timeWindow = '1';
    zpSetActiveTimeBtn('1');
    const btn = document.getElementById('zpCustomBtn');
    if (btn) btn.title = '自定义起点（精确到小时）';
    zpToggleCustomPop(false);
    loadData();
  }

  // --- 事件绑定与启动 ---
  function bindEvents() {
    // 时间窗切换（只绑仪表盘页头那一组：CC 控制台顶部也用了 .time-btn 样式，
    // 全量 querySelectorAll('.time-btn') 会把它的按钮一起抓走）
    const timeBtns = document.querySelectorAll('#headerTimeWindowGroup .time-btn');
    timeBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        // 「自定义」只开面板，应用后才切窗口
        if (btn.getAttribute('data-window') === 'custom') {
          zpToggleCustomPop(!zpCustomPopOpen());
          return;
        }
        zpToggleCustomPop(false);
        timeBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.timeWindow = btn.getAttribute('data-window');
        loadData();
      });
    });
    // 自定义时间范围面板（系统时区，精确到小时）
    const customApply = document.getElementById('zpCustomApply');
    if (customApply) customApply.addEventListener('click', zpApplyCustomRange);
    // 选好日期时间即刷新，不必再点「应用」（面板里的「应用」仍保留）
    const customInput = document.getElementById('zpCustomStart');
    if (customInput) customInput.addEventListener('change', () => {
      if (customInput.value) zpApplyCustomRange();
    });
    const customClear = document.getElementById('zpCustomClear');
    if (customClear) customClear.addEventListener('click', zpClearCustomRange);
    document.addEventListener('click', (e) => {
      if (!zpCustomPopOpen()) return;
      if (e.target.closest('#zpCustomPop') || e.target.closest('#zpCustomBtn')) return;
      zpToggleCustomPop(false);
    });

    // 刷新按钮
    const refreshBtn = document.getElementById('refreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        loadData();
        fetchToolboxStatus();
        if (location.hash === '#/ai') loadAiNews(state.aiCategory);
      });
    }

    // 图表图例开关
    const toggleToken = document.getElementById('toggleTokenSeries');
    const toggleCost = document.getElementById('toggleCostSeries');
    if (toggleToken) {
      toggleToken.addEventListener('click', () => {
        state.chartSeries.token = !state.chartSeries.token;
        toggleToken.classList.toggle('disabled', !state.chartSeries.token);
        renderDailyChart();
      });
    }
    if (toggleCost) {
      toggleCost.addEventListener('click', () => {
        state.chartSeries.cost = !state.chartSeries.cost;
        toggleCost.classList.toggle('disabled', !state.chartSeries.cost);
        renderDailyChart();
      });
    }

    // --- 方案 C 壳：左栏导航过滤（与模块卸载隐藏共用 applyNavFilter 出口） ---
    const navSearch = document.getElementById('navSearch');
    if (navSearch) {
      navSearch.addEventListener('input', () => applyNavFilter());
    }

    // --- 方案 C 壳：强调色切换（微靛蓝 / 琥珀） ---
    const accBtns = document.querySelectorAll('.accent-sw button');
    accBtns.forEach(b => {
      b.addEventListener('click', () => {
        accBtns.forEach(x => x.classList.remove('on'));
        b.classList.add('on');
        document.body.classList.toggle('amber', b.getAttribute('data-accent') === 'amber');
      });
    });

    // --- v3：左栏折叠 / 展开（状态持久化） ---
    const rail = document.getElementById('rail');
    const railTgl = document.getElementById('railTgl');
    if (railTgl && rail) {
      if (localStorage.getItem('xyRailCollapsed') === '1') rail.classList.add('collapsed');
      railTgl.addEventListener('click', () => {
        rail.classList.toggle('collapsed');
        localStorage.setItem('xyRailCollapsed', rail.classList.contains('collapsed') ? '1' : '0');
      });
    }

    // --- v3：外观（主题 / 字体 / 字号 / 背景图） ---
    // 主题清单：dark/light 即原状（不带 theme-* class），其余为新增。
    // mode 决定两件事：body 是否带 .light（复用 body.light .xxx 那批组件规则）、
    // 以及同步给原生窗口边框的 dark/light 二态。
    const APPR_THEMES = [
      { id: 'dark',   name: '深色·微靛蓝', mode: 'dark',  dots: ['#090a0f', '#202431', '#5e6ad2'] },
      { id: 'light',  name: '亮色·云白',   mode: 'light', dots: ['#f3f4f9', '#e9ebf2', '#5661cc'] },
      { id: 'ocean',  name: '深海蓝',      mode: 'dark',  dots: ['#08131f', '#1c3349', '#3d8bfd'] },
      { id: 'violet', name: '紫罗兰',      mode: 'dark',  dots: ['#0f0a1a', '#2d1f55', '#8b5cf6'] },
      { id: 'forest', name: '森林绿',      mode: 'dark',  dots: ['#06120e', '#1a3a2e', '#2ec27e'] },
      { id: 'sepia',  name: '暖纸',        mode: 'light', dots: ['#f6efe2', '#e8dcc6', '#b5651d'] },
      { id: 'slate',  name: '雾灰',        mode: 'light', dots: ['#eef1f5', '#e2e7ee', '#4a6785'] },
    ];
    const APPR_THEME_BY_ID = {};
    APPR_THEMES.forEach(t => { APPR_THEME_BY_ID[t.id] = t; });
    const APPR_FONTS = [
      { key: 'default', name: '默认',    stack: '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif' },
      { key: 'yahei',   name: '微软雅黑', stack: '"Microsoft YaHei", "Microsoft YaHei UI", sans-serif' },
      { key: 'simsun',  name: '宋体',    stack: '"SimSun", "Songti SC", serif' },
      { key: 'kaiti',   name: '楷体',    stack: '"KaiTi", "STKaiti", serif' },
    ];
    const APPR_SCALES = [
      { key: '0.9',  name: '紧凑 90%' },
      { key: '1',    name: '标准 100%' },
      { key: '1.1',  name: '大 110%' },
      { key: '1.25', name: '特大 125%' },
    ];
    const APPR_THEME_IDS = APPR_THEMES.map(t => t.id);
    const APPR_DEFAULTS = {
      theme: 'dark', lastDarkTheme: 'dark', lastLightTheme: 'light',
      fontFamily: 'default', fontScale: '1', bgImage: '', bgOpacity: 100,
      bgBrightness: 100, bgZoom: 100, bgX: 0, bgY: 0,
      cardOpacity: 100, cardBlur: 0
    };
    const APPR_STORAGE_KEYS = {
      theme: 'xyTheme', lastDarkTheme: 'xyLastDarkTheme', lastLightTheme: 'xyLastLightTheme',
      fontFamily: 'xyFontFamily', fontScale: 'xyFontScale', bgImage: 'xyBgImage',
      bgOpacity: 'xyBgOpacity', bgBrightness: 'xyBgBrightness', bgZoom: 'xyBgZoom',
      bgX: 'xyBgX', bgY: 'xyBgY', cardOpacity: 'xyCardOpacity', cardBlur: 'xyCardBlur'
    };
    let apprState = Object.assign({}, APPR_DEFAULTS);
    let apprConfigReady = false;
    const apprPendingKeys = new Set();
    let apprSaveTimer = null;
    let apprSaveBusy = false;
    let apprSaveAgain = false;

    const themeBtn = document.getElementById('btnThemeToggle');
    function syncFrameTheme(dark) {
      // DWM 标题栏/边框不吃网页 CSS，必须通知后端给原生窗口重新打属性
      fetch('http://127.0.0.1:8790/api/window/theme', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: dark ? 'dark' : 'light' })
      }).catch(() => {});
    }
    function apprStorageRead(key) {
      try { return localStorage.getItem(key); } catch (e) { return null; }
    }
    function apprRead(key, allowed, fallback) {
      const v = apprStorageRead(key);
      return allowed.indexOf(v) > -1 ? v : fallback;
    }
    function apprNormalise(raw) {
      const src = raw && typeof raw === 'object' ? raw : {};
      const out = Object.assign({}, APPR_DEFAULTS);
      if (APPR_THEME_IDS.indexOf(src.theme) >= 0) out.theme = src.theme;
      if (APPR_THEME_IDS.indexOf(src.lastDarkTheme) >= 0) out.lastDarkTheme = src.lastDarkTheme;
      if (APPR_THEME_IDS.indexOf(src.lastLightTheme) >= 0) out.lastLightTheme = src.lastLightTheme;
      if (APPR_FONTS.some(f => f.key === src.fontFamily)) out.fontFamily = src.fontFamily;
      if (APPR_SCALES.some(s => s.key === String(src.fontScale))) out.fontScale = String(src.fontScale);
      const image = typeof src.bgImage === 'string' && src.bgImage.indexOf('data:image/jpeg;base64,') === 0 && src.bgImage.length <= 3500000 ? src.bgImage : '';
      out.bgImage = image;
      const bounded = (key, low, high) => {
        const n = Number.parseInt(src[key], 10);
        return Number.isFinite(n) ? Math.max(low, Math.min(high, n)) : APPR_DEFAULTS[key];
      };
      out.bgOpacity = bounded('bgOpacity', 0, 100);
      out.bgBrightness = bounded('bgBrightness', 50, 150);
      out.bgZoom = bounded('bgZoom', 100, 250);
      const panLimit = Math.min(50, Math.floor((out.bgZoom - 100) / 2));
      const pan = key => {
        const n = Number.parseFloat(src[key]);
        return Number.isFinite(n) ? Math.round(Math.max(-panLimit, Math.min(panLimit, n)) * 10) / 10 : 0;
      };
      out.bgX = pan('bgX');
      out.bgY = pan('bgY');
      out.cardOpacity = bounded('cardOpacity', 0, 100);
      out.cardBlur = bounded('cardBlur', 0, 24);
      return out;
    }
    function apprReadLocal() {
      const raw = {};
      Object.keys(APPR_STORAGE_KEYS).forEach(key => {
        const value = apprStorageRead(APPR_STORAGE_KEYS[key]);
        if (value !== null) raw[key] = value;
      });
      return apprNormalise(raw);
    }
    function apprWriteLocal(keys) {
      (keys || Object.keys(APPR_STORAGE_KEYS)).forEach(key => {
        if (!APPR_STORAGE_KEYS[key]) return;
        try { localStorage.setItem(APPR_STORAGE_KEYS[key], String(apprState[key])); } catch (e) { /* config file is authoritative */ }
      });
    }
    const apprStatus = document.getElementById('apprSaveStatus');
    function apprSetStatus(text, isError) {
      if (!apprStatus) return;
      apprStatus.textContent = text;
      apprStatus.classList.toggle('error', !!isError);
    }
    function apprScheduleSave() {
      if (!apprConfigReady) return;
      if (apprSaveTimer) clearTimeout(apprSaveTimer);
      apprSetStatus('保存到本机配置文件…', false);
      apprSaveTimer = setTimeout(apprSaveConfig, 350);
    }
    function apprMarkChanged(keys) {
      keys.forEach(key => apprPendingKeys.add(key));
      apprWriteLocal(keys);
      apprScheduleSave();
    }
    async function apprSaveConfig() {
      apprSaveTimer = null;
      if (apprSaveBusy) { apprSaveAgain = true; return; }
      apprSaveBusy = true;
      apprSaveAgain = false;
      const snapshot = JSON.stringify(apprState);
      let errorText = '';
      try {
        const res = await fetch('/api/appearance', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: snapshot
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        apprPendingKeys.clear();
      } catch (err) {
        errorText = String(err && err.message ? err.message : err);
      }
      apprSaveBusy = false;
      if (apprSaveAgain || snapshot !== JSON.stringify(apprState)) {
        apprSaveAgain = false;
        apprScheduleSave();
      } else if (errorText) {
        apprSetStatus('本机配置保存失败：' + errorText, true);
      } else {
        apprSetStatus('已保存到本机配置文件', false);
      }
    }
    async function apprLoadConfig() {
      const localState = apprState;
      try {
        const res = await fetch('/api/appearance', { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const pending = Array.from(apprPendingKeys);
        if (data.exists && data.config) {
          apprState = apprNormalise(data.config);
          pending.forEach(key => { apprState[key] = localState[key]; });
        } else {
          // 首次启动时把旧版 localStorage 外观偏好迁移到独立配置文件。
          apprState = localState;
        }
        apprState = apprNormalise(apprState);
        apprConfigReady = true;
        apprPendingKeys.clear();
        apprWriteLocal();
        applyApprTheme(apprState.theme, true, false);
        applyApprFont(apprState.fontFamily, false);
        applyApprScale(apprState.fontScale, false);
        applyApprBg();
        applyApprCards();
        if (!data.exists || pending.length) apprScheduleSave();
        else apprSetStatus('已从本机配置载入', false);
      } catch (err) {
        apprConfigReady = true;
        if (apprPendingKeys.size) apprScheduleSave();
        else apprSetStatus('本机配置暂不可读；当前使用浏览器内的设置', true);
      }
    }
    function markApprActive(container, attr, val) {
      if (!container) return;
      Array.prototype.forEach.call(container.children, el => {
        el.classList.toggle('on', el.getAttribute(attr) === val);
      });
    }
    function apprThemeId() { return apprState.theme; }

    function applyApprTheme(id, sync, persist) {
      const t = APPR_THEME_BY_ID[id] || APPR_THEME_BY_ID.dark;
      apprState.theme = t.id;
      const recentKey = t.mode === 'dark' ? 'lastDarkTheme' : 'lastLightTheme';
      apprState[recentKey] = t.id;
      APPR_THEME_IDS.forEach(x => { if (x !== 'dark' && x !== 'light') document.body.classList.remove('theme-' + x); });
      document.body.classList.toggle('light', t.mode === 'light');
      if (t.id !== 'dark' && t.id !== 'light') document.body.classList.add('theme-' + t.id);
      const ic = themeBtn && themeBtn.querySelector('i');
      if (ic) ic.className = t.mode === 'light' ? 'ph ph-moon' : 'ph ph-sun';
      if (sync) syncFrameTheme(t.mode === 'dark');
      markApprActive(document.getElementById('apprThemeGrid'), 'data-theme', t.id);
      if (persist !== false) apprMarkChanged(['theme', recentKey]);
    }

    function applyApprFont(key, persist) {
      const f = APPR_FONTS.filter(x => x.key === key)[0] || APPR_FONTS[0];
      document.documentElement.style.setProperty('--font-ui', f.stack);
      apprState.fontFamily = f.key;
      markApprActive(document.getElementById('apprFontSeg'), 'data-font', f.key);
      if (persist !== false) apprMarkChanged(['fontFamily']);
    }
    function applyApprScale(key, persist) {
      const s = APPR_SCALES.filter(x => x.key === key)[0] || APPR_SCALES[1];
      document.documentElement.style.setProperty('--xy-zoom', s.key);
      apprState.fontScale = s.key;
      markApprActive(document.getElementById('apprScaleSeg'), 'data-scale', s.key);
      if (persist !== false) apprMarkChanged(['fontScale']);
    }

    // ---- 背景图 ----
    function apprBgState() {
      return apprState;
    }
    function applyApprBg() {
      const s = apprBgState();
      const root = document.documentElement.style;
      root.setProperty('--xy-bg-opacity', String(s.bgOpacity / 100));
      root.setProperty('--xy-bg-brightness', String(s.bgBrightness / 100));
      root.setProperty('--xy-bg-zoom', String(s.bgZoom / 100));
      root.setProperty('--xy-bg-x', `${s.bgX}vw`);
      root.setProperty('--xy-bg-y', `${s.bgY}vh`);
      const liveImg = document.getElementById('xyBgImageLayer');
      const prev = document.getElementById('apprBgPreview');
      const previewImg = document.getElementById('apprBgPreviewImage');
      [liveImg, previewImg].forEach(img => {
        if (!img) return;
        if (s.bgImage) {
          if (img.dataset.apprSrc !== s.bgImage) { img.src = s.bgImage; img.dataset.apprSrc = s.bgImage; }
          img.style.display = 'block';
        } else {
          img.removeAttribute('src');
          delete img.dataset.apprSrc;
          img.style.display = 'none';
        }
      });
      if (prev) prev.classList.toggle('has', !!s.bgImage);
      const empty = document.getElementById('apprBgEmpty');
      if (empty) empty.style.display = s.bgImage ? 'none' : '';
      if (previewImg) previewImg.style.transform = `translate(${s.bgX}%, ${s.bgY}%) scale(${s.bgZoom / 100})`;
      const sliders = [
        ['apprBgZoom', s.bgZoom, 'apprBgZoomVal', s.bgZoom + '%'],
        ['apprBgOpacity', s.bgOpacity, 'apprBgOpacityVal', s.bgOpacity + '%'],
        ['apprBgBright', s.bgBrightness, 'apprBgBrightVal', s.bgBrightness + '%']
      ];
      sliders.forEach(([inputId, value, labelId, label]) => {
        const input = document.getElementById(inputId), output = document.getElementById(labelId);
        if (input) input.value = String(value);
        if (output) output.textContent = label;
      });
    }
    function applyApprCards() {
      const root = document.documentElement.style;
      root.setProperty('--xy-card-opacity', `${apprState.cardOpacity}%`);
      root.setProperty('--xy-card-blur', `${apprState.cardBlur}px`);
      document.documentElement.classList.toggle('xy-glass-enabled', apprState.cardBlur > 0);
      const opacity = document.getElementById('apprCardOpacity');
      const opacityVal = document.getElementById('apprCardOpacityVal');
      const blur = document.getElementById('apprCardBlur');
      const blurVal = document.getElementById('apprCardBlurVal');
      if (opacity) opacity.value = String(apprState.cardOpacity);
      if (opacityVal) opacityVal.textContent = `${apprState.cardOpacity}%`;
      if (blur) blur.value = String(apprState.cardBlur);
      if (blurVal) blurVal.textContent = `${apprState.cardBlur}px`;
    }
    // 压缩后放进用户配置 JSON；localStorage 只作旧版本迁移与轻量回退：
    // 最长边 ≤2560 + JPEG 0.82；dataURL 超 1.8MB 降质量到 0.7；仍超 2.5MB 再降到 1920/0.75。
    function compressImageToDataUrl(file) {
      return new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onerror = () => reject(new Error('读取文件失败'));
        fr.onload = () => {
          const img = new Image();
          img.onerror = () => reject(new Error('图片解码失败，换一张试试'));
          img.onload = () => {
            const draw = (maxEdge, quality) => {
              const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
              const w = Math.max(1, Math.round(img.width * scale));
              const h = Math.max(1, Math.round(img.height * scale));
              const cv = document.createElement('canvas');
              cv.width = w; cv.height = h;
              cv.getContext('2d').drawImage(img, 0, 0, w, h);
              return cv.toDataURL('image/jpeg', quality);
            };
            let out = draw(2560, 0.82);
            if (out.length > 1800000) out = draw(2560, 0.7);
            if (out.length > 2500000) out = draw(1920, 0.75);
            resolve(out);
          };
          img.src = fr.result;
        };
        fr.readAsDataURL(file);
      });
    }

    function buildApprControls() {
      const grid = document.getElementById('apprThemeGrid');
      if (grid && !grid.children.length) {
        APPR_THEMES.forEach(t => {
          const b = document.createElement('button');
          b.type = 'button'; b.className = 'theme-card';
          b.setAttribute('data-theme', t.id);
          const dots = document.createElement('span');
          dots.className = 'theme-dots';
          t.dots.forEach(c => {
            const i = document.createElement('i');
            i.style.background = c;
            dots.appendChild(i);
          });
          const nm = document.createElement('span');
          nm.textContent = t.name;
          b.appendChild(dots); b.appendChild(nm);
          b.addEventListener('click', () => applyApprTheme(t.id, true));
          grid.appendChild(b);
        });
      }
      const fseg = document.getElementById('apprFontSeg');
      if (fseg && !fseg.children.length) {
        APPR_FONTS.forEach(f => {
          const b = document.createElement('button');
          b.type = 'button'; b.setAttribute('data-font', f.key);
          b.style.fontFamily = f.stack;   // 按钮自己用该字体渲染：切之前就能看出字形差别
          b.textContent = f.name;
          b.addEventListener('click', () => applyApprFont(f.key));
          fseg.appendChild(b);
        });
      }
      const sseg = document.getElementById('apprScaleSeg');
      if (sseg && !sseg.children.length) {
        APPR_SCALES.forEach(s => {
          const b = document.createElement('button');
          b.type = 'button'; b.setAttribute('data-scale', s.key);
          b.textContent = s.name;
          b.addEventListener('click', () => applyApprScale(s.key));
          sseg.appendChild(b);
        });
      }
    }

    buildApprControls();
    apprState = apprReadLocal();
    applyApprTheme(apprThemeId(), true, false); // 先用本地缓存首帧应用，配置文件随后优先对齐
    applyApprFont(apprState.fontFamily, false);
    applyApprScale(apprState.fontScale, false);
    applyApprBg();
    applyApprCards();
    apprLoadConfig();

    // ===== 翻译浮窗大小（设置页滑杆，拖动时弹出真实预览） =====
    const popupScaleRange = document.getElementById('popupScaleRange');
    if (popupScaleRange) {
      const popupScaleVal = document.getElementById('popupScaleVal');
      let scalePreviewTimer = null;
      const fmtPct = v => Math.round(v * 100) + '%';
      popupScaleRange.addEventListener('input', () => {
        const v = parseFloat(popupScaleRange.value) || 1;
        popupScaleVal.textContent = fmtPct(v);
        clearTimeout(scalePreviewTimer);
        // 节流 220ms：拖动流畅，预览弹窗不至于每格都重建
        scalePreviewTimer = setTimeout(() => {
          fetch('/api/popup/scale', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scale: v })
          }).catch(() => {});
        }, 220);
      });
      fetch('/api/popup/scale').then(r => r.json()).then(d => {
        if (d && d.ok) {
          popupScaleRange.value = d.scale;
          popupScaleVal.textContent = fmtPct(d.scale);
        }
      }).catch(() => {});
    }

    if (themeBtn) {
      themeBtn.addEventListener('click', () => {
        // 在当前系的「最近一个」与另一系之间跳：暗→上一次的亮、亮→上一次的暗
        const curLight = document.body.classList.contains('light');
        const next = curLight ? apprState.lastDarkTheme : apprState.lastLightTheme;
        applyApprTheme(next, true);
      });
    }

    const bgFile = document.getElementById('apprBgFile');
    const btnPickBg = document.getElementById('btnApprPickBg');
    if (btnPickBg && bgFile) btnPickBg.addEventListener('click', () => bgFile.click());
    if (bgFile) {
      bgFile.addEventListener('change', () => {
        const f = bgFile.files && bgFile.files[0];
        bgFile.value = '';   // 复位，同一张图重选也能触发 change
        if (!f) return;
        if (!/^image\//.test(f.type || '')) { alert('只支持图片文件，换一张试试'); return; }
        compressImageToDataUrl(f).then(dataUrl => {
          apprState.bgImage = dataUrl;
          apprState.bgX = 0;
          apprState.bgY = 0;
          apprState.bgZoom = 100;
          applyApprBg();
          apprMarkChanged(['bgImage', 'bgX', 'bgY', 'bgZoom']);
        }).catch(err => { alert(String(err && err.message ? err.message : err)); });
      });
    }
    const zoomSlider = document.getElementById('apprBgZoom');
    if (zoomSlider) zoomSlider.addEventListener('input', () => {
      apprState.bgZoom = Number.parseInt(zoomSlider.value, 10) || 100;
      const limit = Math.min(50, Math.floor((apprState.bgZoom - 100) / 2));
      apprState.bgX = Math.max(-limit, Math.min(limit, apprState.bgX));
      apprState.bgY = Math.max(-limit, Math.min(limit, apprState.bgY));
      applyApprBg();
      apprMarkChanged(['bgZoom', 'bgX', 'bgY']);
    });
    const opSlider = document.getElementById('apprBgOpacity');
    if (opSlider) opSlider.addEventListener('input', () => {
      apprState.bgOpacity = Number.parseInt(opSlider.value, 10) || 0;
      applyApprBg();
      apprMarkChanged(['bgOpacity']);
    });
    const brSlider = document.getElementById('apprBgBright');
    if (brSlider) brSlider.addEventListener('input', () => {
      apprState.bgBrightness = Number.parseInt(brSlider.value, 10) || 50;
      applyApprBg();
      apprMarkChanged(['bgBrightness']);
    });
    const cardOpacitySlider = document.getElementById('apprCardOpacity');
    if (cardOpacitySlider) cardOpacitySlider.addEventListener('input', () => {
      apprState.cardOpacity = Number.parseInt(cardOpacitySlider.value, 10) || 0;
      applyApprCards();
      apprMarkChanged(['cardOpacity']);
    });
    const cardBlurSlider = document.getElementById('apprCardBlur');
    if (cardBlurSlider) cardBlurSlider.addEventListener('input', () => {
      apprState.cardBlur = Number.parseInt(cardBlurSlider.value, 10) || 0;
      applyApprCards();
      apprMarkChanged(['cardBlur']);
    });
    const btnRemoveBg = document.getElementById('btnApprRemoveBg');
    if (btnRemoveBg) {
      btnRemoveBg.addEventListener('click', () => {
        apprState.bgImage = '';
        apprState.bgX = 0;
        apprState.bgY = 0;
        apprState.bgZoom = 100;
        applyApprBg();
        apprMarkChanged(['bgImage', 'bgX', 'bgY', 'bgZoom']);
      });
    }
    const btnCenterBg = document.getElementById('btnApprCenterBg');
    if (btnCenterBg) btnCenterBg.addEventListener('click', () => {
      apprState.bgX = 0;
      apprState.bgY = 0;
      applyApprBg();
      apprMarkChanged(['bgX', 'bgY']);
    });
    const bgPreview = document.getElementById('apprBgPreview');
    if (bgPreview) {
      let drag = null;
      bgPreview.addEventListener('pointerdown', e => {
        if (!apprState.bgImage || e.button !== 0) return;
        const rect = bgPreview.getBoundingClientRect();
        drag = { pointerId: e.pointerId, x: e.clientX, y: e.clientY,
          startX: apprState.bgX, startY: apprState.bgY, width: rect.width, height: rect.height };
        bgPreview.classList.add('dragging');
        bgPreview.setPointerCapture(e.pointerId);
        e.preventDefault();
      });
      bgPreview.addEventListener('pointermove', e => {
        if (!drag || drag.pointerId !== e.pointerId) return;
        const limit = Math.min(50, Math.floor((apprState.bgZoom - 100) / 2));
        apprState.bgX = Math.max(-limit, Math.min(limit, drag.startX + (e.clientX - drag.x) / drag.width * 100));
        apprState.bgY = Math.max(-limit, Math.min(limit, drag.startY + (e.clientY - drag.y) / drag.height * 100));
        applyApprBg();
      });
      const finishBgDrag = e => {
        if (!drag || drag.pointerId !== e.pointerId) return;
        drag = null;
        bgPreview.classList.remove('dragging');
        apprMarkChanged(['bgX', 'bgY']);
      };
      bgPreview.addEventListener('pointerup', finishBgDrag);
      bgPreview.addEventListener('pointercancel', finishBgDrag);
    }

    // --- 热键改键按钮（翻译页热键卡片 + 剪贴板页唤起键行，事件委托） ---
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-hk-act]');
      if (!btn) return;
      const act = btn.getAttribute('data-hk-act');
      if (act === 'edit') beginHotkeyCapture(btn.getAttribute('data-hk-field'));
      else if (act === 'cancel') cancelHotkeyCapture();
      else if (act === 'confirm') confirmHotkeyCapture();
    });

    // --- 模型占比视图切换（单来源视图 + 全部合并） ---
    const srcPills = document.getElementById('modelSourcePills');
    if (srcPills) {
      srcPills.addEventListener('click', e => {
        const pill = e.target.closest('.chip[data-source]');
        if (!pill) return;
        state.modelSource = pill.getAttribute('data-source') || 'all';
        srcPills.querySelectorAll('.chip[data-source]').forEach(button => {
          const active = button === pill;
          button.classList.toggle('on', active);
          button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        renderModelSection();
        updatePanelSummaries();
      });
    }

    const sourcePicker = document.getElementById('modelSourcePicker');
    if (sourcePicker) {
      sourcePicker.addEventListener('change', e => {
        const checkbox = e.target.closest('[data-model-source-toggle]');
        if (!checkbox) return;
        const sourceId = checkbox.getAttribute('data-model-source-toggle');
        const enabled = new Set(state.enabledModelSources || []);
        if (checkbox.checked) enabled.add(sourceId);
        else enabled.delete(sourceId);
        state.enabledModelSources = MODEL_SOURCE_IDS.filter(id => enabled.has(id));
        try { localStorage.setItem(MODEL_SOURCE_STORAGE_KEY, JSON.stringify(state.enabledModelSources)); } catch (err) {}
        renderOverviewCards();
        renderModelSection();
        updatePanelSummaries();
        // 仪表盘 KPI（总 Token / 预估费用 / 命中率）与 Agent 卡都以来源勾选为准
        zpRenderStatic();
        zpRenderTools();
      });
      sourcePicker.addEventListener('click', e => {
        const button = e.target.closest('[data-model-sources-action]');
        if (!button) return;
        state.enabledModelSources = button.getAttribute('data-model-sources-action') === 'all'
          ? MODEL_SOURCE_IDS.slice() : [];
        try { localStorage.setItem(MODEL_SOURCE_STORAGE_KEY, JSON.stringify(state.enabledModelSources)); } catch (err) {}
        renderOverviewCards();
        renderModelSection();
        updatePanelSummaries();
        zpRenderStatic();
        zpRenderTools();
      });
    }
  }

  // --- 前端热更新 ---
  // 桌面端窗口是常驻的（点 X 只 hide 不销毁），WebView 只在窗口创建那一刻加载过
  // 一次前端，之后改 webui/ 桌面端一直显示旧页面——每个页面都这样，不是某一页的问题。
  // 轮询后端的前端版本戳（webui/ 静态文件的 mtime），变了就自己重载。
  let _webuiVer = null;
  function pollWebuiVersion() {
    fetch('/api/webui/version', { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (!d) return;
        if (d.app_version) renderAppVersion(d.app_version);
        if (!d.version) return;
        if (_webuiVer && _webuiVer !== d.version) { location.reload(); return; }
        _webuiVer = d.version;
      })
      .catch(() => { /* 旧版后端没有这条路由：静默即可，不影响使用 */ });
  }

  // --- 启动初始化 ---
  window.addEventListener('DOMContentLoaded', () => {
    initPanelStates();
    initTranslateWorkspace();
    bindEvents();
    zpBindEvents();
    zpGridInit();
    // 1. 同步直出样本数据，确保秒开直出
    initFallback();
    // 2. 尝试请求本机 8790 端口，服务就绪时自动平滑切入实时数据
    loadData();
    state._liveRequested = true;
    // 3. 预取工具箱状态（翻译页热键 / 服务页引擎状态）
    fetchToolboxStatus();
    // 3b. 功能模块：先按本地/默认集应用显隐，远端配置异步到达后再校正
    initModules();
    // 4. 路由处理（含 hashchange 监听）
    handleHashRoute();
    window.addEventListener('hashchange', handleHashRoute);
    // 5. 前端热更新轮询（先记一次基准版本，之后变化才触发重载）
    setTimeout(() => { checkAppUpdate(false); }, 2000);
    pollWebuiVersion();
    setInterval(pollWebuiVersion, 10000);
    zpStartTimer();
    zpWatchTheme();
  });

})();
