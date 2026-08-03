/* ============================================================================
 * industry-engine.js — generic, config-driven dashboard engine for the
 * Naxter multi-industry panel (reservations / hospital / sales / …).
 *
 * Each vertical ships a static/data/<id>.js file that sets `window.INDUSTRY`
 * to a config object: { id, label, currency, statuses, tabs[], seed{} }.
 * This engine renders the whole SPA (tabs, KPI cards, charts, filterable
 * tables, the AI call-to-confirm workflow, payments & reports) from that
 * config, persisting all mutations to localStorage so the demo is stateful
 * with no backend database.
 *
 * To go live later: replace IndustryStore's localStorage calls with fetch()
 * to a real API, and flip INDUSTRY.liveCalls to route confirmation calls
 * through /api/make-call. Nothing else in a vertical's config needs to change.
 * ========================================================================== */
(function () {
'use strict';

/* ----------------------------- store ------------------------------------ *
 * Two backends behind one interface:
 *   - DEMO (default): seed data persisted to localStorage (per-browser).
 *   - LIVE (cfg.live): records fetched from /api/dash/<id>/<collection> — real
 *     bookings captured from AI phone calls + staff entries, shared server-side.
 *     Reference catalogs (doctors/branches) come from /api/dash/<id>/refdata;
 *     derived collections (e.g. patients) are computed by cfg.derive[coll](App).
 * ------------------------------------------------------------------------- */
const Store = {
  id: null, cfg: null, data: null, live: false, ref: null,
  liveColls: [], refColls: {}, derivers: {},
  key() { return 'ind:' + this.id + ':v1'; },
  base() { return '/api/dash/' + this.id; },

  async init(cfg) {
    this.id = cfg.id; this.cfg = cfg;
    this.live = !!cfg.live;
    if (this.live) {
      this.liveColls = cfg.liveCollections || [];
      this.refColls = cfg.refCollections || {};
      this.derivers = cfg.derive || {};
      this.data = {};
      await this.reloadAll(true);
      return;
    }
    let raw = null;
    try { raw = localStorage.getItem(this.key()); } catch (e) {}
    if (raw) { try { this.data = JSON.parse(raw); } catch (e) { this.data = null; } }
    if (!this.data) this.seed();
    if (!this.data._activity) this.data._activity = [];
  },

  async _get(path) {
    const r = await fetch(this.base() + path, { headers: { 'Accept': 'application/json' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  },
  async _post(path, body) {
    const r = await fetch(this.base() + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    });
    return r.json();
  },
  async reloadAll(withRef) {
    if (withRef || !this.ref) { try { this.ref = await this._get('/refdata'); } catch (e) { this.ref = this.ref || {}; } }
    for (const c of this.liveColls) {
      try { this.data[c] = await this._get('/' + c); } catch (e) { this.data[c] = this.data[c] || []; }
    }
  },

  seed() {
    this.data = JSON.parse(JSON.stringify(this.cfg.seed || {}));
    this.data._activity = (this.cfg.seedActivity || []).slice();
    this.persist();
  },
  reset() { if (!this.live) this.seed(); },
  persist() { if (this.live) return; try { localStorage.setItem(this.key(), JSON.stringify(this.data)); } catch (e) {} },

  all(coll) {
    if (this.refColls[coll]) return ((this.ref || {})[this.refColls[coll]] || []).slice();
    if (this.derivers[coll]) { try { return this.derivers[coll](App) || []; } catch (e) { return []; } }
    return (this.data[coll] || []).slice();
  },
  raw(coll) { if (!this.data[coll]) this.data[coll] = []; return this.data[coll]; },
  get(coll, id) { return this.all(coll).find(r => String(r.id) === String(id)); },
  update(coll, id, patch) {
    const r = this.get(coll, id);
    if (r) Object.assign(r, patch);                 // optimistic
    if (this.live) { if (r && this.liveColls.includes(coll)) App._queuePost('/' + coll + '/' + id, patch); }
    else this.persist();
    return r || null;
  },
  insert(coll, rec) {
    if (this.live) {
      if (this.liveColls.includes(coll)) {
        App._queuePost('/' + coll, rec);
        const tmp = Object.assign({ id: 'tmp-' + new Date().getTime(), _pending: true }, rec);
        (this.data[coll] = this.data[coll] || []).unshift(tmp);
        return tmp;
      }
      return rec;
    }
    this.raw(coll).unshift(rec); this.persist(); return rec;
  },
  activity(limit) {
    if (this.live) {
      const src = [];
      this.liveColls.forEach(c => (this.data[c] || []).forEach(r => src.push(r)));
      src.sort((a, b) => String(b.updated || b.created || '').localeCompare(String(a.updated || a.created || '')));
      return src.slice(0, limit || 12).map(r => this._liveActivity(r));
    }
    const a = (this.data._activity || []).slice();
    return limit ? a.slice(0, limit) : a;
  },
  _liveActivity(r) {
    const ai = (r.source === 'AI call' || r.source === 'AI agent');
    const bad = r.status === 'cancelled' || r.status === 'no_show';
    return {
      icon: ai ? 'phone-call' : 'calendar-plus',
      color: bad ? 'rose' : (r.status === 'completed' ? 'sky' : 'emerald'),
      text: this.cfg.activityText ? this.cfg.activityText(r) : ((r.patient || r.customer || r.ref || '') + ' — ' + (r.status || '')),
      who: r.source || (ai ? 'AI agent' : ''), t: r.updated || r.created,
    };
  },
  pushActivity(ev) {
    if (this.live) return;                          // live feed is synthesized from records
    ev.t = ev.t || new Date().toISOString();
    this.raw('_activity').unshift(ev);
    if (this.data._activity.length > 200) this.data._activity.length = 200;
    this.persist();
  },
};

/* --------------------------- formatting --------------------------------- */
function money(n) {
  const cur = (Store.cfg && Store.cfg.currency) || 'Rs';
  const v = Number(n || 0);
  return cur + ' ' + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
}
function fmtNum(n) { return Number(n || 0).toLocaleString(); }
function todayISO() { return new Date().toISOString().slice(0, 10); }
function nowTime() { return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function statusMeta(key) {
  const st = (Store.cfg.statuses || {})[key];
  if (!st) return { label: key || '-', color: 'muted' };
  return { label: st.label || key, color: st.color || 'muted' };
}
function statusBadge(key) {
  const m = statusMeta(key);
  return `<span class="px-2 py-0.5 rounded-md text-[11px] border font-medium whitespace-nowrap badge-${m.color}">${esc(m.label)}</span>`;
}

/* badge color classes mapped to the theme tokens already in base.html */
const BADGE_CSS = `
.badge-emerald{background:hsl(var(--emerald-bg));color:hsl(var(--emerald-fg));border-color:hsl(var(--emerald-fg)/.2)}
.badge-rose{background:hsl(var(--rose-bg));color:hsl(var(--rose-fg));border-color:hsl(var(--rose-fg)/.2)}
.badge-amber{background:hsl(var(--amber-bg));color:hsl(var(--amber-fg));border-color:hsl(var(--amber-fg)/.2)}
.badge-sky{background:hsl(var(--sky-bg));color:hsl(var(--sky-fg));border-color:hsl(var(--sky-fg)/.2)}
.badge-muted{background:hsl(var(--muted));color:hsl(var(--muted-fg));border-color:hsl(var(--border))}
.kpi-accent-emerald{color:hsl(var(--emerald-fg))}.kpi-accent-rose{color:hsl(var(--rose-fg))}
.kpi-accent-amber{color:hsl(var(--amber-fg))}.kpi-accent-sky{color:hsl(var(--sky-fg))}
`;
const CHART_HEX = {
  emerald: '#10b981', rose: '#f43f5e', amber: '#f59e0b', sky: '#0ea5e9',
  muted: '#94a3b8', violet: '#8b5cf6', teal: '#14b8a6', indigo: '#6366f1',
};

/* ----------------------------- charts ----------------------------------- */
const charts = {};
function themeColors() {
  const dark = document.documentElement.classList.contains('dark');
  return { fg: dark ? '#cbd5e1' : '#475569', grid: dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.05)', dark };
}
function drawChart(canvasId, spec) {
  const ctx = document.getElementById(canvasId);
  if (!ctx || typeof Chart === 'undefined') return;
  const { fg, grid, dark } = themeColors();
  Chart.defaults.color = fg; Chart.defaults.font.family = 'Inter'; Chart.defaults.font.size = 11;
  if (charts[canvasId]) charts[canvasId].destroy();
  const palette = (spec.colors || []).map(c => CHART_HEX[c] || c);
  let datasets, opts;
  if (spec.type === 'doughnut') {
    datasets = [{ data: spec.data, backgroundColor: palette, borderWidth: dark ? 1 : 2, borderColor: dark ? 'hsl(224,13%,8%)' : '#fff' }];
    opts = { responsive: true, plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 }, color: fg, padding: 12 } } }, cutout: '64%' };
  } else if (spec.type === 'line') {
    datasets = (spec.series || []).map((s, i) => ({
      label: s.label, data: s.data, borderColor: CHART_HEX[s.color] || palette[i] || '#0ea5e9',
      backgroundColor: 'transparent', borderWidth: 2, tension: 0.35, pointRadius: 2,
    }));
    opts = { responsive: true, plugins: { legend: { position: 'top', align: 'end', labels: { boxWidth: 10, color: fg } } }, scales: { x: { grid: { display: false, color: grid }, ticks: { color: fg, font: { size: 10 } } }, y: { beginAtZero: true, grid: { color: grid }, ticks: { color: fg, font: { size: 10 } } } } };
  } else { /* bar */
    datasets = (spec.series || [{ label: spec.label || '', data: spec.data, color: spec.colors && spec.colors[0] }]).map((s, i) => ({
      label: s.label, data: s.data,
      backgroundColor: (CHART_HEX[s.color] || palette[i] || '#0ea5e9') + (dark ? 'cc' : 'dd'),
      borderRadius: 4, borderSkipped: false,
    }));
    opts = { responsive: true, plugins: { legend: { display: (datasets.length > 1), position: 'top', align: 'end', labels: { boxWidth: 10, color: fg } } }, scales: { x: { grid: { display: false }, ticks: { color: fg, font: { size: 10 } } }, y: { beginAtZero: true, grid: { color: grid }, ticks: { color: fg, font: { size: 10 } } } } };
  }
  charts[canvasId] = new Chart(ctx, { type: spec.type === 'line' ? 'line' : spec.type === 'doughnut' ? 'doughnut' : 'bar', data: { labels: spec.labels, datasets }, options: opts });
}
function destroyCharts() { Object.keys(charts).forEach(k => { charts[k].destroy(); delete charts[k]; }); }

/* ------------------------------ modal ----------------------------------- */
function modal(title, bodyHtml, footerHtml) {
  closeModal();
  const wrap = document.createElement('div');
  wrap.id = 'ind-modal';
  wrap.className = 'fixed inset-0 z-50 flex items-center justify-center p-4';
  wrap.innerHTML = `
    <div class="absolute inset-0 bg-black/40" onclick="IndustryApp._closeModal()"></div>
    <div class="relative bg-card border border-border rounded-xl shadow-2xl w-full max-w-lg max-h-[85vh] overflow-hidden flex flex-col">
      <div class="px-5 py-3.5 border-b border-border flex items-center justify-between">
        <h3 class="text-sm font-semibold text-fg">${title}</h3>
        <button onclick="IndustryApp._closeModal()" class="text-muted-fg hover:text-fg"><i data-lucide="x" class="w-4 h-4"></i></button>
      </div>
      <div class="p-5 overflow-y-auto text-sm">${bodyHtml}</div>
      ${footerHtml ? `<div class="px-5 py-3 border-t border-border flex items-center justify-end gap-2">${footerHtml}</div>` : ''}
    </div>`;
  document.body.appendChild(wrap);
  if (window.lucide) lucide.createIcons();
}
function closeModal() { const m = document.getElementById('ind-modal'); if (m) m.remove(); }

/* ============================= the app ================================== */
const App = {
  cfg: null, activeTab: null, filters: {},

  async boot(id) {
    const cfg = window.INDUSTRY;
    if (!cfg || cfg.id !== id) { document.getElementById('ind-view').innerHTML = '<div class="text-rose-fg p-6">Dashboard data file failed to load.</div>'; return; }
    this.cfg = cfg;
    // inject badge css once
    if (!document.getElementById('ind-badge-css')) { const s = document.createElement('style'); s.id = 'ind-badge-css'; s.textContent = BADGE_CSS; document.head.appendChild(s); }
    try {
      await Store.init(cfg);
    } catch (e) {
      document.getElementById('ind-view').innerHTML = '<div class="text-rose-fg p-6">Could not load live data — ' + esc(String((e && e.message) || e)) + '</div>';
      return;
    }
    if (cfg.live) this._applyLiveChrome();
    this.renderTabs();
    this.renderSideNav();
    const first = (cfg.tabs[0] || {}).id;
    this.show(first);
    if (cfg.live) this._startPolling();
  },
  _applyLiveChrome() {
    const b = document.getElementById('demo-banner');
    if (b) {
      b.className = 'px-4 py-2 rounded-md text-xs bg-emerald-bg text-emerald-fg border border-emerald-fg/20 flex items-center gap-2';
      b.innerHTML = '<i data-lucide="radio" class="w-3.5 h-3.5 shrink-0"></i><span><strong>Live data</strong> — records are captured from real phone calls and staff entries in real time; the reference catalog (doctors, branches) is editable demo data. This view auto-refreshes.</span>';
      if (window.lucide) lucide.createIcons();
    }
    const rb = document.getElementById('btn-reset'); if (rb) rb.remove();
  },
  _startPolling() {
    if (this._poll) clearInterval(this._poll);
    this._poll = setInterval(() => this.refreshLive(), 4000);
  },
  async refreshLive() {
    if (!Store.live) return;
    const before = JSON.stringify(Store.liveColls.map(c => Store.data[c] || []));
    try { await Store.reloadAll(false); } catch (e) { return; }
    if (typeof setUpdatedAt === 'function') setUpdatedAt();
    const after = JSON.stringify(Store.liveColls.map(c => Store.data[c] || []));
    if (before !== after) this._rerenderPreservingInput();
  },
  _rerenderPreservingInput() {
    const inp = document.getElementById('tbl-search');
    const hadFocus = inp && document.activeElement === inp;
    const pos = inp ? inp.selectionStart : null;
    this.show(this.activeTab);
    if (hadFocus) { const ni = document.getElementById('tbl-search'); if (ni) { ni.focus(); if (pos != null) { try { ni.setSelectionRange(pos, pos); } catch (e) {} } } }
  },
  _queuePost(path, body) {
    Store._post(path, body).then(() => { this._scheduleLiveRefresh(); }).catch(() => {});
  },
  _scheduleLiveRefresh() {
    clearTimeout(this._lrt);
    this._lrt = setTimeout(() => this.refreshLive(), 400);
  },

  // expose helpers for config functions
  all: (c) => Store.all(c), get: (c, i) => Store.get(c, i), ref: () => Store.ref || {}, money, fmtNum, todayISO, statusBadge, statusMeta,

  renderTabs() {
    const bar = document.getElementById('ind-tabs');
    bar.innerHTML = this.cfg.tabs.map(t =>
      `<button data-tab="${t.id}" onclick="IndustryApp.show('${t.id}')"
         class="ind-tab px-3.5 py-2.5 text-sm font-medium border-b-2 -mb-px flex items-center gap-1.5 whitespace-nowrap transition-colors">
         <i data-lucide="${t.icon || 'square'}" class="w-3.5 h-3.5"></i>${esc(t.label)}</button>`
    ).join('');
  },
  renderSideNav() {
    const nav = document.getElementById('ind-nav');
    if (!nav) return;
    nav.innerHTML = this.cfg.tabs.map(t =>
      `<a data-snav="${t.id}" href="#${t.id}" onclick="IndustryApp.show('${t.id}');return false;"
        class="flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors text-muted-fg hover:bg-accent hover:text-fg">
        <i data-lucide="${t.icon || 'square'}" class="w-4 h-4 shrink-0"></i><span>${esc(t.label)}</span></a>`
    ).join('');
  },
  _markActive() {
    document.querySelectorAll('.ind-tab').forEach(b => {
      const on = b.dataset.tab === this.activeTab;
      b.className = 'ind-tab px-3.5 py-2.5 text-sm font-medium border-b-2 -mb-px flex items-center gap-1.5 whitespace-nowrap transition-colors ' +
        (on ? 'border-primary text-fg' : 'border-transparent text-muted-fg hover:text-fg');
    });
    document.querySelectorAll('[data-snav]').forEach(a => {
      const on = a.dataset.snav === this.activeTab;
      a.className = 'flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors ' +
        (on ? 'bg-accent text-accent-fg font-medium' : 'text-muted-fg hover:bg-accent hover:text-fg');
    });
  },

  tab(id) { return this.cfg.tabs.find(t => t.id === id); },

  show(id) {
    const t = this.tab(id); if (!t) return;
    this.activeTab = id;
    this._markActive();
    destroyCharts();
    const view = document.getElementById('ind-view');
    const fn = this['view_' + t.type];
    view.innerHTML = fn ? fn.call(this, t) : '<div class="text-muted-fg">Unknown view</div>';
    if (window.lucide) lucide.createIcons();
    // post-render hooks (charts)
    if (t.type === 'overview') this._drawOverview(t);
    if (t.type === 'reports') this._drawReports(t);
    setUpdatedAt && setUpdatedAt();
  },
  refresh() { this.show(this.activeTab); },

  /* ----------------------------- OVERVIEW ------------------------------- */
  view_overview(t) {
    const kpis = (t.kpis || []).map(k => {
      const v = k.value(this); const sub = k.sub ? k.sub(this) : '';
      return `<div class="border border-border rounded-xl bg-card p-4 hover:border-border-strong transition-colors">
        <div class="flex items-center justify-between"><div class="text-xs text-muted-fg font-medium">${esc(k.label)}</div>
          <div class="w-7 h-7 rounded-md bg-accent flex items-center justify-center"><i data-lucide="${k.icon || 'activity'}" class="w-3.5 h-3.5 text-muted-fg"></i></div></div>
        <div class="mt-3"><div class="text-xl font-semibold ${k.color ? 'kpi-accent-' + k.color : 'text-fg'}">${v}</div>
          <div class="text-[11px] text-muted-fg mt-0.5">${sub}</div></div></div>`;
    }).join('');
    const charts = (t.charts || []).map((c, i) =>
      `<div class="border border-border rounded-xl bg-card ${c.wide ? 'lg:col-span-2' : ''}">
        <div class="px-5 py-3.5 border-b border-border"><h3 class="text-sm font-semibold text-fg">${esc(c.title)}</h3>
        ${c.sub ? `<p class="text-[11px] text-muted-fg mt-0.5">${esc(c.sub)}</p>` : ''}</div>
        <div class="p-5"><canvas id="ov-chart-${i}" style="max-height:240px"></canvas></div></div>`
    ).join('');
    return `
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">${kpis}</div>
      <div class="grid lg:grid-cols-3 gap-4">${charts}</div>
      ${this._activityCard(t.feedTitle || 'Live activity', t.feedLimit || 12)}`;
  },
  _drawOverview(t) {
    (t.charts || []).forEach((c, i) => drawChart('ov-chart-' + i, c.data(this)));
  },
  _activityCard(title, limit) {
    const feed = Store.activity(limit);
    const rows = feed.length ? feed.map(a => `
      <div class="flex items-start gap-3 px-5 py-2.5 border-t border-border">
        <div class="w-7 h-7 rounded-md flex items-center justify-center shrink-0 badge-${a.color || 'sky'}"><i data-lucide="${a.icon || 'activity'}" class="w-3.5 h-3.5"></i></div>
        <div class="min-w-0 flex-1"><div class="text-sm text-fg">${esc(a.text)}</div>
          <div class="text-[11px] text-muted-fg mt-0.5">${esc(a.who || '')}${a.who ? ' · ' : ''}${this._ago(a.t)}</div></div>
      </div>`).join('') : '<div class="px-5 py-8 text-center text-muted-fg text-sm">No activity yet</div>';
    return `<div class="border border-border rounded-xl bg-card overflow-hidden">
      <div class="px-5 py-3.5 border-b border-border flex items-center justify-between">
        <h3 class="text-sm font-semibold text-fg">${esc(title)}</h3>
        <span class="text-[11px] text-muted-fg">auto-updates as actions happen</span></div>
      <div>${rows}</div></div>`;
  },
  _ago(iso) {
    const d = (Date.now() - new Date(iso).getTime()) / 1000;
    if (isNaN(d)) return '';
    if (d < 60) return 'just now';
    if (d < 3600) return Math.floor(d / 60) + 'm ago';
    if (d < 86400) return Math.floor(d / 3600) + 'h ago';
    return Math.floor(d / 86400) + 'd ago';
  },

  /* ------------------------------ TABLE --------------------------------- */
  view_table(t) {
    const rows = this._filteredRows(t);
    const summary = t.summary ? `<div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">${t.summary(this).map(s =>
      `<div class="border border-border rounded-xl bg-card p-4"><div class="text-xs text-muted-fg font-medium">${esc(s.label)}</div>
       <div class="text-lg font-semibold mt-1 ${s.color ? 'kpi-accent-' + s.color : 'text-fg'}">${s.value}</div></div>`).join('')}</div>` : '';
    // filter chips
    const statusOpts = t.statusFilter ? this._statusFilterBar(t) : '';
    const bulk = t.bulk ? t.bulk.map(b =>
      `<button onclick="IndustryApp.runBulk('${t.id}','${b.id}')" class="text-sm px-3 py-1.5 rounded-md border border-border bg-card hover:bg-card-hover flex items-center gap-1.5 transition-colors">
        <i data-lucide="${b.icon || 'zap'}" class="w-3.5 h-3.5"></i>${esc(b.label)}</button>`).join('') : '';
    const head = t.columns.map(c => `<th class="text-left px-4 py-2.5 font-medium ${c.right ? 'text-right' : ''}">${esc(c.label)}</th>`).join('') +
      (t.actions ? '<th class="text-right px-4 py-2.5 font-medium">Actions</th>' : '');
    const body = rows.length ? rows.map(r => {
      const tds = t.columns.map(c => {
        let v = c.render ? c.render(r, this) : esc(r[c.key]);
        return `<td class="px-4 py-2.5 ${c.right ? 'text-right' : ''} ${c.mono ? 'font-mono text-xs' : ''}">${v}</td>`;
      }).join('');
      const acts = t.actions ? '<td class="px-4 py-2.5 text-right whitespace-nowrap">' + t.actions.filter(a => !a.when || a.when(r)).map(a =>
        `<button onclick="IndustryApp.runAction('${t.id}','${a.id}','${r.id}')" title="${esc(a.label)}"
          class="inline-flex items-center justify-center w-7 h-7 rounded-md border border-border hover:bg-card-hover text-${a.color || 'muted'}-fg ml-1 transition-colors">
          <i data-lucide="${a.icon}" class="w-3.5 h-3.5"></i></button>`).join('') + '</td>' : '';
      const click = t.detail ? `onclick="IndustryApp.openDetail('${t.id}','${r.id}')" class="border-t border-border cursor-pointer"` : 'class="border-t border-border"';
      return `<tr ${click}>${tds}${acts}</tr>`;
    }).join('') : `<tr><td colspan="20" class="px-4 py-10 text-center text-muted-fg text-sm">No records</td></tr>`;

    return `${summary}
      <div class="flex flex-wrap items-center gap-2 mb-3">
        <div class="relative flex-1 min-w-[200px] max-w-xs">
          <i data-lucide="search" class="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-fg"></i>
          <input id="tbl-search" oninput="IndustryApp.onSearch('${t.id}', this.value)" placeholder="Search…"
            class="w-full pl-8 pr-3 py-1.5 text-sm border border-border rounded-md bg-card text-fg" value="${esc(this.filters[t.id + ':q'] || '')}">
        </div>
        ${bulk}
        <span class="text-xs text-muted-fg ml-auto">${rows.length} record${rows.length === 1 ? '' : 's'}</span>
      </div>
      ${statusOpts}
      <div class="border border-border rounded-xl bg-card overflow-hidden">
        <div class="overflow-x-auto"><table class="w-full text-sm">
          <thead class="bg-muted/50"><tr class="text-[11px] text-muted-fg uppercase tracking-wider">${head}</tr></thead>
          <tbody class="text-fg">${body}</tbody></table></div></div>`;
  },
  _statusFilterBar(t) {
    const coll = Store.all(t.collection);
    const counts = {}; coll.forEach(r => { counts[r[t.statusKey || 'status']] = (counts[r[t.statusKey || 'status']] || 0) + 1; });
    const cur = this.filters[t.id + ':status'] || '';
    const chip = (val, label, n) => `<button onclick="IndustryApp.setStatusFilter('${t.id}','${val}')"
      class="px-2.5 py-1 rounded-md text-xs border font-medium transition-colors ${cur === val ? 'bg-accent text-accent-fg border-border-strong' : 'bg-card text-muted-fg border-border hover:bg-card-hover'}">${esc(label)} <span class="opacity-60">${n}</span></button>`;
    let chips = chip('', 'All', coll.length);
    Object.keys(this.cfg.statuses || {}).forEach(k => { if (counts[k]) chips += chip(k, statusMeta(k).label, counts[k]); });
    return `<div class="flex flex-wrap gap-1.5 mb-3">${chips}</div>`;
  },
  _filteredRows(t) {
    let rows = Store.all(t.collection);
    const q = (this.filters[t.id + ':q'] || '').toLowerCase().trim();
    const st = this.filters[t.id + ':status'] || '';
    if (st) rows = rows.filter(r => r[t.statusKey || 'status'] === st);
    if (q && t.searchKeys) rows = rows.filter(r => t.searchKeys.some(k => String(r[k] || '').toLowerCase().includes(q)));
    if (t.sort) rows.sort(t.sort);
    return rows;
  },
  onSearch(tabId, v) { this.filters[tabId + ':q'] = v; const t = this.tab(tabId); const view = document.getElementById('ind-view'); view.innerHTML = this.view_table(t); if (window.lucide) lucide.createIcons(); const inp = document.getElementById('tbl-search'); if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); } },
  setStatusFilter(tabId, v) { this.filters[tabId + ':status'] = v; this.refresh(); },

  /* --------------------------- actions ---------------------------------- */
  runAction(tabId, actId, recId) {
    event && event.stopPropagation && event.stopPropagation();
    const t = this.tab(tabId); const a = t.actions.find(x => x.id === actId); const r = Store.get(t.collection, recId);
    if (!a || !r) return;
    a.run(r, this);
    this.refresh();
  },
  runBulk(tabId, bulkId) {
    const t = this.tab(tabId); const b = t.bulk.find(x => x.id === bulkId);
    if (b) b.run(this);
  },
  openDetail(tabId, recId) {
    const t = this.tab(tabId); const r = Store.get(t.collection, recId);
    if (!t.detail || !r) return;
    const d = t.detail(r, this);
    modal(d.title, d.body, d.footer || `<button onclick="IndustryApp._closeModal()" class="px-3 py-1.5 text-sm border border-border rounded-md hover:bg-card-hover">Close</button>`);
  },
  _closeModal: closeModal,

  // modal helpers exposed so verticals can build custom forms (e.g. booking)
  modal: modal, closeModal: closeModal,

  // mutation helpers used by config action handlers
  setStatus(coll, id, status, activity) {
    Store.update(coll, id, { status });
    if (activity) Store.pushActivity(activity);
  },
  update(coll, id, patch) { return Store.update(coll, id, patch); },
  insert(coll, rec) { return Store.insert(coll, rec); },
  log(ev) { Store.pushActivity(ev); },
  toast: (m, k) => toast(m, k),

  /* ----------------------- CALL-TO-CONFIRM QUEUE ------------------------ */
  view_callqueue(t) {
    const q = this._queueRows(t);
    const counters = this._callCounters(t);
    return `
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="border border-border rounded-xl bg-card p-4"><div class="text-xs text-muted-fg font-medium">In queue</div><div class="text-xl font-semibold text-fg mt-2" id="cq-pending">${q.length}</div></div>
        <div class="border border-border rounded-xl bg-card p-4"><div class="text-xs text-muted-fg font-medium">Confirmed</div><div class="text-xl font-semibold kpi-accent-emerald mt-2" id="cq-confirmed">${counters.confirmed}</div></div>
        <div class="border border-border rounded-xl bg-card p-4"><div class="text-xs text-muted-fg font-medium">No answer</div><div class="text-xl font-semibold kpi-accent-amber mt-2" id="cq-noanswer">${counters.no_answer}</div></div>
        <div class="border border-border rounded-xl bg-card p-4"><div class="text-xs text-muted-fg font-medium">Cancelled</div><div class="text-xl font-semibold kpi-accent-rose mt-2" id="cq-cancelled">${counters.cancelled}</div></div>
      </div>
      <div class="border border-border rounded-xl bg-card overflow-hidden">
        <div class="px-5 py-3.5 border-b border-border flex items-center justify-between flex-wrap gap-2">
          <div><h3 class="text-sm font-semibold text-fg">${esc(t.title || 'AI confirmation calls')}</h3>
          <p class="text-[11px] text-muted-fg mt-0.5">${esc(t.desc || 'The AI voice agent calls each contact and updates the status automatically.')}</p></div>
          <div class="flex items-center gap-2">
            <button id="cq-start" onclick="IndustryApp.startCalls('${t.id}')" class="px-3 py-1.5 bg-primary text-primary-fg rounded-md text-sm font-medium hover:opacity-90 flex items-center gap-1.5"><i data-lucide="phone-call" class="w-3.5 h-3.5"></i> Start AI calls</button>
            <button id="cq-stop" onclick="IndustryApp.stopCalls()" class="hidden px-3 py-1.5 border border-border rounded-md text-sm hover:bg-card-hover flex items-center gap-1.5"><i data-lucide="square" class="w-3.5 h-3.5"></i> Stop</button>
          </div>
        </div>
        <div class="px-5 py-2.5 border-b border-border hidden" id="cq-live">
          <div class="flex items-center gap-2 text-sm"><span class="relative flex h-2.5 w-2.5"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-fg opacity-60"></span><span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-fg"></span></span>
          <span id="cq-live-text" class="text-fg"></span></div>
        </div>
        <div id="cq-list">${this._queueListHtml(t, q)}</div>
      </div>`;
  },
  _queueRows(t) {
    const pend = t.pendingStatuses || ['pending'];
    return Store.all(t.collection).filter(r => pend.includes(r[t.statusKey || 'status']));
  },
  _callCounters(t) {
    const all = Store.all(t.collection); const c = { confirmed: 0, no_answer: 0, cancelled: 0 };
    all.forEach(r => { const s = r[t.statusKey || 'status']; if (s in c) c[s]++; });
    return c;
  },
  _queueListHtml(t, q) {
    if (!q.length) return '<div class="px-5 py-10 text-center text-muted-fg text-sm">Queue empty — every record is confirmed or closed. 🎉</div>';
    return q.map(r => `
      <div class="px-5 py-3 border-t border-border flex items-center gap-3" id="cq-row-${r.id}">
        <div class="w-8 h-8 rounded-full bg-accent text-accent-fg flex items-center justify-center text-xs font-medium shrink-0">${esc(String(r[t.nameKey] || '?')[0]).toUpperCase()}</div>
        <div class="min-w-0 flex-1">
          <div class="text-sm font-medium text-fg truncate">${esc(r[t.nameKey])}</div>
          <div class="text-[11px] text-muted-fg font-mono">${esc(r[t.phoneKey] || '')} · ${esc(t.subtitle ? t.subtitle(r) : (r.ref || ''))}</div>
        </div>
        <div class="text-xs" id="cq-st-${r.id}">${statusBadge(r[t.statusKey || 'status'])}</div>
        <button onclick="IndustryApp.callOne('${t.id}','${r.id}')" class="text-xs px-2.5 py-1 rounded-md border border-border hover:bg-card-hover flex items-center gap-1"><i data-lucide="phone" class="w-3 h-3"></i> Call now</button>
      </div>`).join('');
  },
  _calling: false,
  async startCalls(tabId) {
    if (this._calling) return;
    const t = this.tab(tabId);
    const q = this._queueRows(t);
    if (!q.length) { toast('Queue is empty'); return; }
    this._calling = true;
    document.getElementById('cq-start').classList.add('hidden');
    document.getElementById('cq-stop').classList.remove('hidden');
    document.getElementById('cq-live').classList.remove('hidden');
    for (const r of q) {
      if (!this._calling) break;
      await this._simulateCall(t, r);
    }
    this._calling = false;
    const sb = document.getElementById('cq-start'), st = document.getElementById('cq-stop'), lv = document.getElementById('cq-live');
    if (sb) sb.classList.remove('hidden'); if (st) st.classList.add('hidden'); if (lv) lv.classList.add('hidden');
    if (this.activeTab === tabId) this.show(tabId);
  },
  stopCalls() { this._calling = false; },
  async callOne(tabId, recId) {
    const t = this.tab(tabId); const r = Store.get(t.collection, recId); if (!r) return;
    document.getElementById('cq-live').classList.remove('hidden');
    await this._simulateCall(t, r);
    document.getElementById('cq-live').classList.add('hidden');
    this.show(tabId);
  },
  _sleep(ms) { return new Promise(res => setTimeout(res, ms)); },
  async _simulateCall(t, r) {
    const name = r[t.nameKey]; const stKey = t.statusKey || 'status';
    const liveText = document.getElementById('cq-live-text');
    const stCell = document.getElementById('cq-st-' + r.id);
    const setLive = (m) => { if (liveText) liveText.textContent = m; };
    // ringing
    if (stCell) stCell.innerHTML = '<span class="px-2 py-0.5 rounded-md text-[11px] border font-medium badge-sky">📞 ringing…</span>';
    setLive('Dialing ' + name + ' (' + (r[t.phoneKey] || '') + ')…');
    await this._sleep(700 + Math.random() * 600);
    // outcome
    const roll = Math.random();
    let outcome;
    if (roll < 0.12) outcome = 'no_answer';
    else if (roll < 0.22) outcome = 'cancelled';
    else outcome = 'confirmed';
    if (outcome === 'no_answer') {
      setLive(name + ' did not answer — will retry later.');
      if (stCell) stCell.innerHTML = statusBadge('no_answer');
      Store.update(t.collection, r.id, { [stKey]: 'no_answer' });
      Store.pushActivity({ icon: 'phone-missed', color: 'amber', text: `No answer from ${name} — ${t.subjectLabel} ${r.ref || ''} still pending`, who: 'AI agent' });
    } else if (outcome === 'cancelled') {
      setLive(name + ' asked to cancel.');
      if (stCell) stCell.innerHTML = statusBadge('cancelled');
      Store.update(t.collection, r.id, { [stKey]: 'cancelled' });
      Store.pushActivity({ icon: 'x-circle', color: 'rose', text: `${name} cancelled ${t.subjectLabel} ${r.ref || ''}`, who: 'AI agent' });
    } else {
      setLive(name + ' confirmed. ✓');
      if (stCell) stCell.innerHTML = statusBadge('confirmed');
      const patch = { [stKey]: 'confirmed' };
      if (t.onConfirm) t.onConfirm(r, patch, this);
      Store.update(t.collection, r.id, patch);
      Store.pushActivity({ icon: 'phone-call', color: 'emerald', text: `${name} confirmed ${t.subjectLabel} ${r.ref || ''}`, who: 'AI agent' });
    }
    // update counters live
    const c = this._callCounters(t);
    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setTxt('cq-confirmed', c.confirmed); setTxt('cq-noanswer', c.no_answer); setTxt('cq-cancelled', c.cancelled);
    setTxt('cq-pending', this._queueRows(t).length);
    await this._sleep(450);
  },

  /* ----------------------------- REPORTS -------------------------------- */
  view_reports(t) {
    const cards = (t.metrics ? t.metrics(this) : []).map(m =>
      `<div class="border border-border rounded-xl bg-card p-4"><div class="text-xs text-muted-fg font-medium">${esc(m.label)}</div>
       <div class="text-xl font-semibold mt-1 ${m.color ? 'kpi-accent-' + m.color : 'text-fg'}">${m.value}</div>
       ${m.sub ? `<div class="text-[11px] text-muted-fg mt-0.5">${esc(m.sub)}</div>` : ''}</div>`).join('');
    const charts = (t.charts || []).map((c, i) =>
      `<div class="border border-border rounded-xl bg-card ${c.wide ? 'lg:col-span-2' : ''}">
        <div class="px-5 py-3.5 border-b border-border"><h3 class="text-sm font-semibold text-fg">${esc(c.title)}</h3></div>
        <div class="p-5"><canvas id="rp-chart-${i}" style="max-height:260px"></canvas></div></div>`).join('');
    const tables = (t.tables ? t.tables(this) : []).map(tb =>
      `<div class="border border-border rounded-xl bg-card overflow-hidden">
        <div class="px-5 py-3.5 border-b border-border"><h3 class="text-sm font-semibold text-fg">${esc(tb.title)}</h3></div>
        <div class="overflow-x-auto"><table class="w-full text-sm"><thead class="bg-muted/50"><tr class="text-[11px] text-muted-fg uppercase tracking-wider">
          ${tb.head.map((h, i) => `<th class="text-left px-5 py-2.5 font-medium ${i ? 'text-right' : ''}">${esc(h)}</th>`).join('')}</tr></thead>
        <tbody>${tb.rows.map(row => `<tr class="border-t border-border">${row.map((c, i) => `<td class="px-5 py-2.5 ${i ? 'text-right font-mono text-xs' : 'text-fg'}">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>`).join('');
    return `<div class="grid grid-cols-2 md:grid-cols-4 gap-3">${cards}</div>
      <div class="grid lg:grid-cols-2 gap-4">${charts}</div>${tables}`;
  },
  _drawReports(t) { (t.charts || []).forEach((c, i) => drawChart('rp-chart-' + i, c.data(this))); },

  /* --------------------------- export / reset --------------------------- */
  exportCurrent() {
    const t = this.tab(this.activeTab);
    let coll = t && t.collection;
    if (!coll) { // pick first table tab
      const tt = this.cfg.tabs.find(x => x.collection); coll = tt && tt.collection;
    }
    if (!coll) { toast('Nothing to export here', 'error'); return; }
    const rows = Store.all(coll);
    if (!rows.length) { toast('No rows to export', 'error'); return; }
    const keys = Object.keys(rows[0]);
    const csv = [keys.join(',')].concat(rows.map(r => keys.map(k => {
      let v = r[k]; if (v == null) v = ''; v = String(v).replace(/"/g, '""');
      return /[",\n]/.test(v) ? `"${v}"` : v;
    }).join(','))).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = this.cfg.id + '-' + coll + '-' + todayISO() + '.csv'; a.click();
    toast('Exported ' + rows.length + ' rows');
  },
  resetData() {
    if (Store.live) { toast('This dashboard runs on live data — reset is not available', 'error'); return; }
    if (!confirm('Reset this dashboard to the original dummy demo data? Your local changes will be lost.')) return;
    Store.reset(); this.refresh(); toast('Demo data reset');
  },
};

window.IndustryApp = App;
// Expose formatting helpers as globals so per-vertical data files (which run in
// their own IIFE) can call money()/statusBadge()/statusMeta()/fmtNum() directly
// inside their column/kpi render closures.
window.money = money;
window.fmtNum = fmtNum;
window.statusBadge = statusBadge;
window.statusMeta = statusMeta;
window.esc = esc;
})();
