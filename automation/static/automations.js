/* Naxter Automations — n8n-style builder.
 * No sidebars: add nodes via the "+" on a node (or "Add node") -> picker popup;
 * double-click a node -> config + run-data popup. React Flow from UMD globals.
 * Colours/grid/edges are CSS-variable driven (light + dark) — see automations.html.
 */
(function () {
  "use strict";

  // apply saved theme ASAP (avoid flash)
  try { if (localStorage.getItem("auto-theme") === "dark") document.documentElement.classList.add("dark"); } catch (e) {}

  var API = "/api/automations";
  var IN = "inp";

  var React, ReactDOM, RF, RFC, h, useEffect, useCallback;

  var S = {
    catalog: [], byType: {}, list: [], statsById: {}, wf: null, root: null, inst: null,
    getFlow: null, applyFlow: null, addRfNode: null, addEdge: null, patchNode: null, setNodeStatuses: null,
    picker: null, modalNode: null, lastRun: null, _n: 0,
  };

  // ─────────────────────────────────────────────────────────────────────
  // TOP-8 n8n NODE DEFINITIONS — the single source of truth for how each
  // node looks on the canvas. One entry maps a backend catalog `type` to:
  //   color    → background of the 56×56 solid block
  //   icon     → pure-white Lucide glyph rendered inside the block
  //   title    → 14px name shown beneath the block
  //   subtitle → 12px muted line under the title
  // title/subtitle/icon mirror the backend NODE_CATALOG (so palette + picker
  // + canvas stay in sync); colour + the SVG path live here because the
  // Python engine doesn't own presentation. See NodeBox() for the mapping.
  // ─────────────────────────────────────────────────────────────────────
  var TOP_NODES = [
    { type: "webhookTrigger",  title: "Webhook",           subtitle: "Starts workflow on event", color: "#8B5CF6", icon: "webhook" },
    { type: "scheduleTrigger", title: "Schedule Trigger",  subtitle: "Runs on a timer",          color: "#8B5CF6", icon: "clock" },
    { type: "googleSheets",    title: "Google Sheets",     subtitle: "Read/Write rows",          color: "#0F9D58", icon: "file-spreadsheet" },
    { type: "httpRequest",     title: "HTTP Request",      subtitle: "Call external API",        color: "#10B981", icon: "globe" },
    { type: "if",              title: "IF",                subtitle: "Split into True/False",    color: "#3B82F6", icon: "git-branch" },
    { type: "code",            title: "Code",              subtitle: "Custom JS/Python",         color: "#F59E0B", icon: "terminal" },
    { type: "set",             title: "Edit Fields (Set)", subtitle: "Modify data",              color: "#14B8A6", icon: "pencil" },
    { type: "openAi",          title: "OpenAI",            subtitle: "Generate/Parse text",      color: "#25262B", icon: "sparkles" },
  ];

  // per-node solid colours for the icon square — TOP_NODES drives the 8;
  // the remaining catalog nodes keep their own accents.
  var COLORS = {
    manualTrigger: "#9092FF",   // purple
    formTrigger: "#F59E0B",     // amber
    wait: "#6366F1",            // indigo
    respondToWebhook: "#06B6D4",// cyan
  };
  TOP_NODES.forEach(function (n) { COLORS[n.type] = n.color; });
  function ncol(type) { return COLORS[type] || "#9CA3AF"; }

  // exact Lucide thin-line icon paths
  var ICONS = {
    "mouse-pointer-click": '<path d="M14 4.1 12 6"/><path d="m5.1 8-2.9-.8"/><path d="m6 12-1.9 2"/><path d="M7.2 2.2 8 5.1"/><path d="m9 9 5 12 1.8-5.2L21 14Z"/>',
    "webhook": '<path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2"/><path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06"/><path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8"/>',
    "clipboard-list": '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "timer": '<line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/>',
    "git-branch": '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "pencil": '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "file-spreadsheet": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M8 13h2"/><path d="M14 13h2"/><path d="M8 17h2"/><path d="M14 17h2"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
    "sparkles": '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .962 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.962 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/>',
    "reply": '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
    "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  };

  // ------------------------------------------------------------- helpers
  function el(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function jsonStr(v) { try { return typeof v === "string" ? v : JSON.stringify(v); } catch (e) { return String(v); } }
  function clone(v) { return v == null ? v : JSON.parse(JSON.stringify(v)); }
  function uid(type) { return type + "-" + Math.random().toString(36).slice(2, 7); }
  function toast(msg, ok) { var t = el("toast"); t.textContent = msg; t.className = "fixed bottom-4 right-4 px-3 py-2 rounded-md text-sm shadow-lg z-[60] " + (ok ? "bg-emerald-600 text-white" : "bg-rose-600 text-white"); t.style.display = "block"; setTimeout(function () { t.style.display = "none"; }, 2600); }
  function copyText(t) { if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(t).then(function () { toast("Copied", true); }); return; } var ta = document.createElement("textarea"); ta.value = t; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); toast("Copied", true); } catch (e) { toast("Copy failed", false); } document.body.removeChild(ta); }
  async function jget(u) { var r = await fetch(u); var j = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }
  async function jsend(u, m, b) { var r = await fetch(u, { method: m, headers: { "Content-Type": "application/json" }, body: b != null ? JSON.stringify(b) : undefined }); var j = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }

  async function loadCatalog() { var d = await jget(API + "/_node-catalog"); S.catalog = d.nodes || []; S.byType = {}; S.catalog.forEach(function (n) { S.byType[n.type] = n; }); }

  // ------------------------------------------------------------- React Flow canvas
  function iconSvg(name, color) { return h("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round", strokeLinejoin: "round", className: "n8n-ico", style: { color: color }, dangerouslySetInnerHTML: { __html: ICONS[name] || ICONS.box } }); }

  function NodeBox(props) {
    var d = props.data || {}, outs = d.outputs || 1, isTrig = d.group === "trigger", accent = ncol(d.atype);
    var bcls = "n8n-block" + (props.selected ? " sel" : "") + (d.status ? " " + d.status : "");
    // everything that the wires plug into lives INSIDE the block (handles routed to the square)
    var kids = [iconSvg(d.icon, "#FFFFFF")];
    if (!isTrig) kids.push(h(RF.Handle, { key: "in", type: "target", position: RF.Position.Left }));
    if (outs === 2) {
      kids.push(h(RF.Handle, { key: "o0", type: "source", id: "0", position: RF.Position.Right, style: { top: "34%" } }));
      kids.push(h(RF.Handle, { key: "o1", type: "source", id: "1", position: RF.Position.Right, style: { top: "66%" } }));
    } else kids.push(h(RF.Handle, { key: "o", type: "source", id: "0", position: RF.Position.Right }));
    function plus(idx, top) {
      kids.push(h("div", { key: "st" + idx, className: "n8n-stub", style: { top: top } }));
      kids.push(h("div", { key: "pl" + idx, className: "n8n-plus nodrag", style: { top: top }, title: "Add next node", onClick: function (e) { e.stopPropagation(); openPicker({ source: props.id, output: idx }); } }, "+"));
    }
    if (outs === 2) { plus(0, "34%"); plus(1, "66%"); } else plus(0, "50%");
    return h("div", { className: "n8n-node" },
      h("div", { key: "block", className: bcls, style: { background: accent } }, kids),
      h("div", { key: "tx", className: "n8n-textwrap" },
        h("div", { className: "n8n-name" }, d.label || d.typeName),
        h("div", { className: "n8n-type" }, d.desc || d.typeName)));
  }
  var nodeTypes = { box: NodeBox };

  function Canvas() {
    var ns = RF.useNodesState([]); var nodes = ns[0], setNodes = ns[1], onNodesChange = ns[2];
    var es = RF.useEdgesState([]); var edges = es[0], setEdges = es[1], onEdgesChange = es[2];
    useEffect(function () { S.getFlow = function () { return { nodes: nodes, edges: edges }; }; }, [nodes, edges]);
    useEffect(function () {
      S.applyFlow = function (n, e) { setNodes(n); setEdges(e); };
      S.addRfNode = function (node) { setNodes(function (x) { return x.concat(node); }); };
      S.addEdge = function (source, handle, target) { setEdges(function (e) { return RF.addEdge({ source: source, target: target, sourceHandle: handle }, e); }); };
      S.patchNode = function (id, patch) { setNodes(function (x) { return x.map(function (n) { return n.id === id ? Object.assign({}, n, { data: Object.assign({}, n.data, patch) }) : n; }); }); };
      S.setNodeStatuses = function (map) { setNodes(function (x) { return x.map(function (n) { return Object.assign({}, n, { data: Object.assign({}, n.data, { status: map[n.id] || "" }) }); }); }); };
    }, [setNodes, setEdges]);
    var onConnect = useCallback(function (c) { setEdges(function (e) { return RF.addEdge({ source: c.source, target: c.target, sourceHandle: c.sourceHandle }, e); }); }, [setEdges]);
    var onInit = useCallback(function (inst) { S.inst = inst; syncGrid(); }, []);
    var onDoubleClick = useCallback(function (e, node) { openNodeModal(node.id); }, []);
    var onMove = useCallback(function (_e, vp) { syncGrid(vp); }, []);
    return h("div", { style: { width: "100%", height: "100%" } },
      h(RFC, {
        nodes: nodes, edges: edges, onNodesChange: onNodesChange, onEdgesChange: onEdgesChange,
        onConnect: onConnect, onInit: onInit, onNodeDoubleClick: onDoubleClick, onMove: onMove,
        nodeTypes: nodeTypes, fitView: true, minZoom: 0.25, deleteKeyCode: ["Backspace", "Delete"],
        proOptions: { hideAttribution: true },
      },
        h(RF.Controls, { showInteractive: false })
      )
    );
  }
  function RootApp() { return h(RF.ReactFlowProvider, null, h(Canvas, null)); }

  // keep the CSS dot grid (on .react-flow__pane) aligned with the viewport (pan + zoom)
  function syncGrid(vp) {
    var p = el("canvas") && el("canvas").querySelector(".react-flow__pane"); if (!p) return;
    vp = vp || ((S.inst && S.inst.getViewport) ? S.inst.getViewport() : { x: 0, y: 0, zoom: 1 });
    var z = vp.zoom || 1;
    p.style.backgroundSize = (20 * z) + "px " + (20 * z) + "px";
    p.style.backgroundPosition = vp.x + "px " + vp.y + "px";
  }
  function mountCanvas() {
    React = window.React; ReactDOM = window.ReactDOM; RF = window.ReactFlow;
    if (!React || !ReactDOM || !RF) throw new Error("React Flow failed to load from CDN (offline or blocked).");
    RFC = RF.default || RF.ReactFlow || RF;
    h = React.createElement; useEffect = React.useEffect; useCallback = React.useCallback;
    S.root = ReactDOM.createRoot(el("canvas"));
    S.root.render(h(RootApp));
    setTimeout(function () { var ok = el("canvas").querySelector(".react-flow"); var ce = el("canvas-empty"); if (ce) { if (ok) ce.style.display = "none"; else { ce.style.display = "grid"; ce.textContent = "Canvas failed to render — open the browser console."; } } updateEmptyState(); syncGrid(); }, 600);
  }

  // ------------------------------------------------------------- our JSON <-> React Flow
  function nodeData(cat, label, params) { return { label: label, typeName: cat.name, desc: cat.description || "", group: cat.group, outputs: cat.outputs || 1, atype: cat.type, icon: cat.icon, parameters: params || {}, status: "" }; }
  function defaultParams(cat) { var p = {}; (cat.params || []).forEach(function (f) { p[f.key] = clone(f.default); }); return p; }
  function toRf(wf) {
    var nodes = (wf.nodes || []).map(function (n) { var cat = S.byType[n.type] || { name: n.type, group: "action", outputs: 1, type: n.type, params: [], icon: "box" }; return { id: n.id, type: "box", position: n.position || { x: 140, y: 140 }, data: nodeData(cat, n.name || cat.name, n.parameters || {}) }; });
    var edges = [];
    Object.keys(wf.connections || {}).forEach(function (src) {
      var main = (wf.connections[src] || {}).main || [];
      main.forEach(function (targets, idx) { (targets || []).forEach(function (t) { edges.push({ id: src + ":" + idx + "->" + t.node, source: src, target: t.node, sourceHandle: String(idx) }); }); });
    });
    return { nodes: nodes, edges: edges };
  }
  function fromRf() {
    var flow = S.getFlow ? S.getFlow() : { nodes: [], edges: [] };
    var nodes = flow.nodes.map(function (n) { return { id: n.id, name: n.data.label, type: n.data.atype, position: n.position, parameters: n.data.parameters || {} }; });
    var connections = {};
    flow.edges.forEach(function (e) { var idx = parseInt(e.sourceHandle || "0", 10) || 0; if (!connections[e.source]) connections[e.source] = { main: [] }; var main = connections[e.source].main; while (main.length <= idx) main.push([]); main[idx].push({ node: e.target, index: 0 }); });
    return { nodes: nodes, connections: connections };
  }
  function loadFlowIntoCanvas() { var rf = toRf(S.wf); if (S.applyFlow) S.applyFlow(rf.nodes, rf.edges); setTimeout(updateEmptyState, 120); }
  function currentRfNode(id) { var f = S.getFlow ? S.getFlow() : { nodes: [] }; return f.nodes.filter(function (n) { return n.id === id; })[0]; }
  function centerPos() { var c = el("canvas"), r = c.getBoundingClientRect(); if (S.inst) { var fn = S.inst.screenToFlowPosition || S.inst.project; if (fn) { var p = fn.call(S.inst, { x: r.left + r.width / 2, y: r.top + r.height / 2 }); if (p) return { x: p.x - 95, y: p.y - 24 }; } } S._n = (S._n || 0) + 1; return { x: 140 + S._n * 26, y: 120 + (S._n % 6) * 24 }; }
  function deleteNode(id) { var f = S.getFlow(); S.applyFlow(f.nodes.filter(function (n) { return n.id !== id; }), f.edges.filter(function (e) { return e.source !== id && e.target !== id; })); setTimeout(updateEmptyState, 80); }
  function updateEmptyState() { var f = S.getFlow ? S.getFlow() : { nodes: [] }; var b = el("btn-addfirst"); if (b) b.classList.toggle("hidden", !!f.nodes.length); }

  // ------------------------------------------------------------- node picker popup
  function openPicker(source) { S.picker = { source: source || null }; el("picker-search").value = ""; renderPickerList(""); el("picker-modal").classList.remove("hidden"); setTimeout(function () { el("picker-search").focus(); }, 30); }
  function closePicker() { el("picker-modal").classList.add("hidden"); S.picker = null; }
  function renderPickerList(filter) {
    filter = (filter || "").toLowerCase();
    var src = S.picker && S.picker.source;
    var items = S.catalog.filter(function (n) { return n.implemented !== false; });
    if (src) items = items.filter(function (n) { return n.group !== "trigger"; });
    items = items.filter(function (n) { return (n.name + " " + (n.description || "") + " " + n.type).toLowerCase().indexOf(filter) !== -1; });
    var groups = { trigger: [], action: [], logic: [] }; items.forEach(function (n) { (groups[n.group] || (groups[n.group] = [])).push(n); });
    var labels = { trigger: "Triggers", action: "Actions", logic: "Logic" }; var html = "";
    Object.keys(groups).forEach(function (g) {
      if (!groups[g].length) return;
      html += '<div class="text-[10px] uppercase tracking-wide text-muted px-2 mt-2 mb-1">' + (labels[g] || g) + "</div>";
      groups[g].forEach(function (n) {
        html += '<button class="picker-item w-full flex items-center gap-2.5 px-2 py-2 rounded-lg text-left" data-type="' + esc(n.type) + '"><span class="w-8 h-8 grid place-items-center rounded-lg shrink-0" style="background:' + ncol(n.type) + '"><i data-lucide="' + esc(n.icon || "box") + '" class="w-4 h-4" style="color:#fff"></i></span><span class="min-w-0"><span class="block text-sm font-medium">' + esc(n.name) + '</span><span class="block text-[11px] text-muted truncate">' + esc(n.description || "") + "</span></span></button>";
      });
    });
    el("picker-list").innerHTML = html || '<div class="text-muted text-sm p-4 text-center">No matching nodes.</div>';
    if (window.lucide) lucide.createIcons();
    el("picker-list").querySelectorAll(".picker-item").forEach(function (b) { b.addEventListener("click", function () { pickNode(b.getAttribute("data-type")); }); });
  }
  function pickNode(type) {
    var cat = S.byType[type]; if (!cat) return;
    var src = S.picker && S.picker.source, pos;
    if (src) { var sn = currentRfNode(src.source); var bx = sn ? sn.position.x : 120, by = sn ? sn.position.y : 150; pos = { x: bx + 270, y: by + (src.output === 1 ? 100 : 0) }; }
    else pos = centerPos();
    var id = uid(type);
    S.addRfNode({ id: id, type: "box", position: pos, data: nodeData(cat, cat.name, defaultParams(cat)) });
    if (src && S.addEdge) S.addEdge(src.source, String(src.output), id);
    closePicker();
    setTimeout(function () { updateEmptyState(); openNodeModal(id); }, 70);
  }

  // ------------------------------------------------------------- node config popup
  function getParam(id, key) { var n = currentRfNode(id); return n ? clone((n.data.parameters || {})[key]) : undefined; }
  function setParam(id, key, v) { var n = currentRfNode(id); var p = Object.assign({}, n ? n.data.parameters : {}); p[key] = v; S.patchNode(id, { parameters: p }); }
  function showWhen(f, params) { if (!f.showWhen) return true; return (f.showWhen.in || []).indexOf(params[f.showWhen.key]) !== -1; }
  function controlsVisibility(cat, key) { return (cat.params || []).some(function (f) { return f.showWhen && f.showWhen.key === key; }); }
  function fieldWrap(label, inner, help) { return '<label class="block mb-4"><span class="nss-label">' + esc(label) + "</span>" + inner + (help ? '<span class="block text-[10px] text-muted mt-1">' + esc(help) + "</span>" : "") + "</label>"; }
  function copyRow(text) { return '<div class="flex gap-1"><input readonly value="' + esc(text) + '" class="inp text-[11px]" onclick="this.select()"><button class="copybtn px-2 rounded-md btn" data-copy="' + esc(text) + '" title="Copy"><i data-lucide="copy" class="w-3.5 h-3.5"></i></button></div>'; }
  function triggerInfo(node) {
    var p = node.data.parameters || {}, origin = window.location.origin, a = node.data.atype;
    if (a === "webhookTrigger") {
      if (!p.path) return '<div class="mb-3 text-[11px]" style="color:#d97706">Save the workflow to generate the webhook URL.</div>';
      var html = '<div class="mb-3"><div class="text-[11px] text-muted mb-1">Webhook URL</div>' + copyRow(origin + "/api/automations/hook/" + p.path);
      if (p.auth === "header secret" && p.secret) html += '<div class="text-[11px] text-muted mt-2 mb-1">Secret header <code>X-Webhook-Secret</code></div>' + copyRow(p.secret);
      return html + '<div class="text-[10px] text-muted mt-2">Toggle <b>Active</b> (top bar) for this URL to fire.</div></div>';
    }
    if (a === "formTrigger") {
      if (!p.path) return '<div class="mb-3 text-[11px]" style="color:#d97706">Save the workflow to generate the form link.</div>';
      var furl = origin + "/automations/form/" + p.path;
      return '<div class="mb-3"><div class="text-[11px] text-muted mb-1">Form link</div>' + copyRow(furl) + '<div class="text-[10px] text-muted mt-2"><a href="' + furl + '" target="_blank" style="color:var(--accent)">Open form ↗</a> — needs the workflow <b>Active</b>.</div></div>';
    }
    return "";
  }
  function renderField(f, val) {
    var c = f.control, key = f.key;
    if (c === "select") { var opts = (f.options || []).map(function (o) { return "<option " + (String(val) === String(o) ? "selected" : "") + ">" + esc(o) + "</option>"; }).join(""); return fieldWrap(f.label, '<select data-key="' + key + '" class="inp">' + opts + "</select>", f.help); }
    if (c === "bool") return '<label class="nss-toggle"><span>' + esc(f.label) + '</span><input type="checkbox" data-key="' + key + '" ' + (val ? "checked" : "") + "></label>";
    if (c === "number") return fieldWrap(f.label, '<input type="number" data-key="' + key + '" class="inp" value="' + esc(val == null ? "" : val) + '">', f.help);
    if (c === "text") return fieldWrap(f.label, '<textarea data-key="' + key + '" rows="3" class="inp font-mono text-xs">' + esc(val == null ? "" : val) + "</textarea>", f.help);
    if (c === "json") return fieldWrap(f.label, '<textarea data-key="' + key + '" data-json="1" rows="3" class="inp font-mono text-xs">' + esc(jsonStr(val == null ? {} : val)) + "</textarea>", "JSON object");
    if (c === "fieldlist" || c === "keyvalue") return fieldListEditor(f, val);
    if (c === "formfields") return formFieldsEditor(f, val);
    return fieldWrap(f.label, '<input data-key="' + key + '" class="inp" value="' + esc(val == null ? "" : val) + '">', f.help);
  }
  function fieldListEditor(f, val) { var list = Array.isArray(val) ? val : []; var rows = list.map(function (item, i) { return '<div class="flex gap-1 mb-1" data-row="' + i + '"><input data-fl="' + f.key + '" data-fi="name" class="inp" style="flex:1" placeholder="field" value="' + esc(item.name || "") + '"><input data-fl="' + f.key + '" data-fi="value" class="inp" style="flex:1.4" placeholder="value" value="' + esc(item.value == null ? "" : item.value) + '"><button data-flrm="' + f.key + '" data-i="' + i + '" class="text-muted px-1">&times;</button></div>'; }).join(""); return '<div class="mb-3"><div class="text-[11px] text-muted mb-1">' + esc(f.label) + '</div><div data-flwrap="' + f.key + '">' + rows + '</div><button data-fladd="' + f.key + '" class="text-[11px]" style="color:var(--accent)">+ add</button></div>'; }
  function formFieldsEditor(f, val) { var list = Array.isArray(val) ? val : [], types = ["text", "email", "number", "textarea", "select"]; var rows = list.map(function (it, i) { var opts = types.map(function (t) { return "<option " + (it.type === t ? "selected" : "") + ">" + t + "</option>"; }).join(""); return '<div class="flex gap-1 mb-1 items-center" data-ffrow="' + i + '"><input data-ff="' + f.key + '" data-fk="label" class="inp" style="flex:1.3" placeholder="Label" value="' + esc(it.label || "") + '"><select data-ff="' + f.key + '" data-fk="type" class="inp" style="flex:1">' + opts + '</select><label class="text-[10px] text-muted flex items-center gap-1"><input type="checkbox" data-ff="' + f.key + '" data-fk="required" ' + (it.required ? "checked" : "") + '>req</label><button data-ffrm="' + f.key + '" data-i="' + i + '" class="text-muted px-1">&times;</button></div>'; }).join(""); return '<div class="mb-3"><div class="text-[11px] text-muted mb-1">' + esc(f.label) + '</div><div data-ffwrap="' + f.key + '">' + rows + '</div><button data-ffadd="' + f.key + '" class="text-[11px]" style="color:var(--accent)">+ add field</button></div>'; }

  function runDataHtml(id) {
    if (!S.lastRun) return '<div class="mt-4 pt-3 text-[11px] text-muted" style="border-top:1px solid var(--panel-border)">Run the workflow to see this node\'s data.</div>';
    var nr = (S.lastRun.node_runs || []).filter(function (n) { return n.node_id === id; })[0];
    if (!nr) return '<div class="mt-4 pt-3 text-[11px] text-muted" style="border-top:1px solid var(--panel-border)">This node didn\'t run last time.</div>';
    var out = nr.output && nr.output.length ? JSON.stringify(nr.output, null, 2) : "(no output)";
    return '<div class="mt-4 pt-3" style="border-top:1px solid var(--panel-border)"><div class="text-[11px] text-muted mb-1">Last run · ' + esc(nr.status) + " · in " + nr.items_in + " / out " + nr.items_out + "</div>" + (nr.error ? '<div class="text-xs mb-2" style="color:#ef4444">' + esc(nr.error) + "</div>" : "") + '<pre class="code">' + esc(out) + "</pre></div>";
  }
  function openNodeModal(id) {
    var node = currentRfNode(id); if (!node) return;
    S.modalNode = id;
    var cat = S.byType[node.data.atype] || { params: [], name: node.data.atype }, accent = ncol(node.data.atype);
    el("node-modal-head").innerHTML = '<div class="nss-id"><span class="nss-icon" style="background:' + accent + '"><i data-lucide="' + esc(cat.icon || "box") + '"></i></span><div class="nss-id-txt"><input id="nm-label" class="nss-title" value="' + esc(node.data.label || "") + '"><div class="nss-sub">' + esc(node.data.desc || cat.description || node.data.atype) + '</div></div></div><div class="nss-actions"><button id="nm-del" class="nss-iconbtn danger" title="Delete node"><i data-lucide="trash-2" class="w-4 h-4"></i></button><button id="nm-close" class="nss-iconbtn" title="Close (Esc)"><i data-lucide="x" class="w-4 h-4"></i></button></div>';
    var params = node.data.parameters || {}, body = "", ti = triggerInfo(node), any = false;
    if (ti) body += ti;
    (cat.params || []).forEach(function (f) { if (showWhen(f, params)) { body += renderField(f, params[f.key]); any = true; } });
    if (!any && !ti) body += '<div class="text-[11px] text-muted mb-3">This node has no settings.</div>';
    body += runDataHtml(id);
    el("node-modal-body").innerHTML = body;
    el("node-modal-foot").innerHTML = '<button id="nm-save" class="btn">Save</button><button id="nm-exec" class="btn btn-primary nss-exec"><i data-lucide="play" class="w-3.5 h-3.5"></i>Execute Node</button>';
    el("node-modal").classList.add("open");
    if (window.lucide) lucide.createIcons();
    var labelInp = el("nm-label"); if (labelInp) labelInp.addEventListener("input", function () { S.patchNode(id, { label: labelInp.value }); });
    el("nm-close").addEventListener("click", closeNodeModal);
    el("nm-del").addEventListener("click", function () { deleteNode(id); closeNodeModal(); });
    el("nm-save").addEventListener("click", function () { save().catch(function (e) { toast("Save failed: " + e.message, false); }); });
    el("nm-exec").addEventListener("click", run);
    wireConfig(el("node-modal-body"), id, cat);
  }
  function closeNodeModal() { el("node-modal").classList.remove("open"); S.modalNode = null; }
  function wireConfig(root, id, cat) {
    root.querySelectorAll("[data-copy]").forEach(function (b) { b.addEventListener("click", function () { copyText(b.getAttribute("data-copy")); }); });
    root.querySelectorAll("[data-key]").forEach(function (inp) {
      var ev = (inp.tagName === "SELECT" || inp.type === "checkbox") ? "change" : "input";
      inp.addEventListener(ev, function () {
        var key = inp.getAttribute("data-key"), v;
        if (inp.type === "checkbox") v = inp.checked;
        else if (inp.getAttribute("data-json")) { try { v = JSON.parse(inp.value || "{}"); inp.style.borderColor = ""; } catch (e) { inp.style.borderColor = "#ef4444"; return; } }
        else if (inp.type === "number") v = inp.value === "" ? "" : Number(inp.value);
        else v = inp.value;
        setParam(id, key, v);
        if (controlsVisibility(cat, key)) openNodeModal(id);
      });
    });
    root.querySelectorAll("[data-fladd]").forEach(function (b) { b.addEventListener("click", function () { var k = b.getAttribute("data-fladd"), a = getParam(id, k) || []; a.push({ name: "", value: "" }); setParam(id, k, a); openNodeModal(id); }); });
    root.querySelectorAll("[data-flrm]").forEach(function (b) { b.addEventListener("click", function () { var k = b.getAttribute("data-flrm"), a = getParam(id, k) || []; a.splice(+b.getAttribute("data-i"), 1); setParam(id, k, a); openNodeModal(id); }); });
    root.querySelectorAll("[data-fl]").forEach(function (inp) { inp.addEventListener("input", function () { var k = inp.getAttribute("data-fl"), a = []; root.querySelector('[data-flwrap="' + k + '"]').querySelectorAll("[data-row]").forEach(function (r) { a.push({ name: r.querySelector('[data-fi="name"]').value, value: r.querySelector('[data-fi="value"]').value }); }); setParam(id, k, a); }); });
    root.querySelectorAll("[data-ffadd]").forEach(function (b) { b.addEventListener("click", function () { var k = b.getAttribute("data-ffadd"), a = getParam(id, k) || []; a.push({ label: "Field", type: "text", required: false }); setParam(id, k, a); openNodeModal(id); }); });
    root.querySelectorAll("[data-ffrm]").forEach(function (b) { b.addEventListener("click", function () { var k = b.getAttribute("data-ffrm"), a = getParam(id, k) || []; a.splice(+b.getAttribute("data-i"), 1); setParam(id, k, a); openNodeModal(id); }); });
    root.querySelectorAll("[data-ff]").forEach(function (inp) { var ev = (inp.tagName === "SELECT" || inp.type === "checkbox") ? "change" : "input"; inp.addEventListener(ev, function () { var k = inp.getAttribute("data-ff"), a = []; root.querySelector('[data-ffwrap="' + k + '"]').querySelectorAll("[data-ffrow]").forEach(function (r) { a.push({ label: r.querySelector('[data-fk="label"]').value, type: r.querySelector('[data-fk="type"]').value, required: r.querySelector('[data-fk="required"]').checked }); }); setParam(id, k, a); }); });
  }

  // ------------------------------------------------------------- tidy / auto-layout
  function tidyLayout() {
    var f = S.getFlow(); if (!f || !f.nodes.length) return;
    var nodes = f.nodes, edges = f.edges, indeg = {}, outAdj = {};
    nodes.forEach(function (n) { indeg[n.id] = 0; });
    edges.forEach(function (e) { (outAdj[e.source] = outAdj[e.source] || []).push(e.target); indeg[e.target] = (indeg[e.target] || 0) + 1; });
    var layer = {}, q = []; nodes.forEach(function (n) { if (!indeg[n.id]) { layer[n.id] = 0; q.push(n.id); } });
    if (!q.length && nodes.length) { layer[nodes[0].id] = 0; q.push(nodes[0].id); }
    var seen = {}, guard = 0;
    while (q.length && guard++ < 9999) { var id = q.shift(); if (seen[id]) continue; seen[id] = 1; (outAdj[id] || []).forEach(function (t) { layer[t] = Math.max(layer[t] || 0, (layer[id] || 0) + 1); q.push(t); }); }
    nodes.forEach(function (n) { if (layer[n.id] == null) layer[n.id] = 0; });
    var cols = {}; nodes.forEach(function (n) { (cols[layer[n.id]] = cols[layer[n.id]] || []).push(n); });
    var newNodes = nodes.map(function (n) { var L = layer[n.id], idx = cols[L].indexOf(n); return Object.assign({}, n, { position: { x: 80 + L * 280, y: 80 + idx * 130 } }); });
    S.applyFlow(newNodes, edges);
    setTimeout(function () { if (S.inst && S.inst.fitView) S.inst.fitView({ padding: 0.2 }); }, 60);
    toast("Tidied up", true);
  }

  // ------------------------------------------------------------- list / header / actions
  async function loadList(selectId) { var d = await jget(API); S.list = d.workflows || []; S.statsById = {}; S.list.forEach(function (w) { S.statsById[w.id] = w.stats; }); el("wf-select").innerHTML = S.list.map(function (w) { return '<option value="' + esc(w.id) + '">' + esc(w.name || w.id) + "</option>"; }).join(""); if (selectId) el("wf-select").value = selectId; }
  async function openWorkflow(id) { S.wf = await jget(API + "/" + id); S.lastRun = null; paintHeader(); paintActive(); loadFlowIntoCanvas(); clearRunlog(); }
  function paintHeader() { el("wf-name").value = S.wf ? (S.wf.name || "") : ""; var st = (S.wf && S.statsById) ? S.statsById[S.wf.id] : null; var meta = S.wf ? ("v" + (S.wf.version || 1)) : ""; if (st) meta += " · executed " + st.total + " times · " + st.succeeded + " ok / " + st.failed + " fail" + (st.last_status ? (" · last " + st.last_status) : ""); el("wf-meta").textContent = meta; if (S.wf) el("wf-select").value = S.wf.id; }
  function paintActive() { var on = !!(S.wf && S.wf.active); el("active-dot").style.background = on ? "#10b981" : "#9CA3AF"; el("active-label").textContent = on ? "Active" : "Inactive"; el("btn-active").style.color = on ? "#10b981" : ""; el("btn-active").style.borderColor = on ? "#10b981" : ""; }
  function clearRunlog() { el("run-status").textContent = ""; el("runlog").innerHTML = "Press <b>Execute Workflow</b> to run."; }

  async function save() {
    if (!S.wf) return;
    var flow = fromRf();
    var d = await jsend(API + "/" + S.wf.id, "PUT", { name: el("wf-name").value || S.wf.name, nodes: flow.nodes, connections: flow.connections });
    S.wf = d; await loadList(d.id); paintHeader();
    (d.nodes || []).forEach(function (n) { if ((n.type === "webhookTrigger" || n.type === "formTrigger") && S.patchNode) S.patchNode(n.id, { parameters: n.parameters }); });
    if (S.modalNode && el("node-modal").classList.contains("open")) openNodeModal(S.modalNode);
    toast("Saved v" + d.version, true);
  }
  async function run() {
    if (!S.wf) return;
    try { await save(); } catch (e) { toast("Save failed: " + e.message, false); return; }
    try {
      var r = await jsend(API + "/" + S.wf.id + "/run", "POST", {}); S.lastRun = r;
      renderRunlog(r);
      var map = {}; (r.node_runs || []).forEach(function (n) { map[n.node_id] = n.status === "failed" ? "fail" : (n.status === "skipped" ? "skip" : "ok"); });
      if (S.setNodeStatuses) S.setNodeStatuses(map);
      if (S.modalNode && el("node-modal").classList.contains("open")) openNodeModal(S.modalNode);
      await loadList(S.wf.id); paintHeader();
    } catch (e) { toast("Run failed: " + e.message, false); }
  }
  function renderRunlog(r) {
    var color = r.status === "success" ? "#10b981" : (r.status === "failed" ? "#ef4444" : "#d97706");
    el("run-status").style.color = color; el("run-status").textContent = "· " + r.status + (r.trigger ? (" (" + r.trigger + ")") : "") + (r.error ? (" — " + r.error) : "");
    var rows = (r.node_runs || []).map(function (n) {
      var dot = n.status === "success" ? "#10b981" : (n.status === "failed" ? "#ef4444" : "#9ca3af");
      var ic = n.status === "skipped" ? "○" : (n.status === "failed" ? "✕" : "●");
      var detail = n.error ? '<span style="color:#ef4444">' + esc(n.error) + "</span>" : '<span class="text-muted">' + esc(jsonStr(n.sample)) + "</span>";
      return '<div class="flex items-start gap-2 py-0.5 cursor-pointer" style="border-bottom:1px solid var(--panel-border)" data-jump="' + esc(n.node_id) + '"><span style="color:' + dot + '">' + ic + '</span><span class="w-28 shrink-0">' + esc(n.name || n.node_id) + '</span><span class="text-muted w-24 shrink-0">' + esc(n.type) + '</span><span class="text-muted w-24 shrink-0">in ' + n.items_in + " · out " + n.items_out + '</span><span class="truncate">' + detail + "</span></div>";
    }).join("");
    el("runlog").innerHTML = rows || '<div class="text-muted">No nodes ran.</div>';
    el("runlog").querySelectorAll("[data-jump]").forEach(function (row) { row.addEventListener("click", function () { openNodeModal(row.getAttribute("data-jump")); }); });
  }
  async function toggleActive() {
    if (!S.wf) return;
    try { await save(); } catch (e) { toast("Save failed: " + e.message, false); return; }
    try { var d = await jsend(API + "/" + S.wf.id + "/activate", "POST", { active: !S.wf.active }); S.wf.active = d.active; paintActive(); toast(d.active ? "Activated — triggers are live" : "Deactivated", true); } catch (e) { toast("Failed: " + e.message, false); }
  }
  async function newWf() { var name = prompt("New workflow name:", "Untitled workflow"); if (!name) return; try { var d = await jsend(API, "POST", { name: name }); await loadList(d.id); await openWorkflow(d.id); toast("Created", true); } catch (e) { toast("Create failed: " + e.message, false); } }
  function exportWf() { if (S.wf) window.open(API + "/" + S.wf.id + "/export", "_blank"); }
  async function doImport(file) { var obj; try { obj = JSON.parse(await file.text()); } catch (e) { toast("Not valid JSON", false); return; } try { var d = await jsend(API + "/import", "POST", obj); await loadList(d.id); await openWorkflow(d.id); toast("Imported as " + d.id, true); } catch (e) { toast("Import failed: " + e.message, false); } }
  async function showHistory() {
    if (!S.wf) return; var d = await jget(API + "/" + S.wf.id + "/versions"); var list = d.versions || [];
    el("history-list").innerHTML = list.length ? list.map(function (v) { return '<div class="flex items-center justify-between px-2 py-2 rounded-md picker-item"><div><div class="text-sm">v' + v.version + '</div><div class="text-[11px] text-muted">' + esc(v.saved_at || "") + " · " + v.nodes + ' nodes</div></div><button data-restore="' + v.version + '" class="btn text-xs">Restore</button></div>'; }).join("") : '<div class="text-muted text-sm p-3">No earlier versions yet — save again to create history.</div>';
    el("history-list").querySelectorAll("[data-restore]").forEach(function (b) { b.addEventListener("click", async function () { try { var d2 = await jsend(API + "/" + S.wf.id + "/restore/" + b.getAttribute("data-restore"), "POST", {}); await loadList(d2.id); await openWorkflow(d2.id); hideHistory(); toast("Restored → v" + d2.version, true); } catch (e) { toast("Restore failed: " + e.message, false); } }); });
    el("history-modal").classList.remove("hidden");
  }
  function hideHistory() { el("history-modal").classList.add("hidden"); }

  function applyThemeIcon() { var dark = document.documentElement.classList.contains("dark"); el("btn-theme").innerHTML = '<i data-lucide="' + (dark ? "sun" : "moon") + '" class="w-3.5 h-3.5"></i>'; if (window.lucide) lucide.createIcons(); }
  function toggleTheme() { var dark = document.documentElement.classList.toggle("dark"); try { localStorage.setItem("auto-theme", dark ? "dark" : "light"); } catch (e) {} applyThemeIcon(); }

  // ------------------------------------------------------------- boot
  async function boot() {
    try {
      await loadCatalog(); mountCanvas(); await loadList();
      el("btn-theme").addEventListener("click", toggleTheme); applyThemeIcon();
      el("btn-save").addEventListener("click", function () { save().catch(function (e) { toast("Save failed: " + e.message, false); }); });
      el("btn-run").addEventListener("click", run);
      el("btn-active").addEventListener("click", toggleActive);
      el("btn-tidy").addEventListener("click", tidyLayout);
      el("btn-new").addEventListener("click", newWf);
      el("btn-export").addEventListener("click", exportWf);
      el("btn-import").addEventListener("click", function () { el("file-import").click(); });
      el("file-import").addEventListener("change", function (e) { if (e.target.files[0]) doImport(e.target.files[0]); e.target.value = ""; });
      el("btn-history").addEventListener("click", showHistory);
      el("btn-addnode").addEventListener("click", function () { openPicker(null); });
      el("btn-addfirst").addEventListener("click", function () { openPicker(null); });
      el("picker-search").addEventListener("input", function (e) { renderPickerList(e.target.value); });
      el("picker-close").addEventListener("click", closePicker);
      el("picker-modal").addEventListener("click", function (e) { if (e.target.id === "picker-modal") closePicker(); });
      el("node-modal").addEventListener("click", function (e) { if (e.target.id === "node-modal") closeNodeModal(); });
      el("history-close").addEventListener("click", hideHistory);
      el("history-modal").addEventListener("click", function (e) { if (e.target.id === "history-modal") hideHistory(); });
      el("wf-select").addEventListener("change", function () { openWorkflow(el("wf-select").value); });
      el("wf-name").addEventListener("input", function () { if (S.wf) S.wf.name = el("wf-name").value; });
      document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closePicker(); closeNodeModal(); hideHistory(); } });
      if (S.list.length) await openWorkflow(S.list[0].id);
      else { var d = await jsend(API, "POST", { name: "My first workflow" }); await loadList(d.id); await openWorkflow(d.id); }
      if (window.lucide) lucide.createIcons();
    } catch (e) { console.error(e); toast("Init error: " + e.message, false); var ce = el("canvas-empty"); if (ce) { ce.style.display = "grid"; ce.textContent = "Failed to load: " + e.message; } }
  }
  document.addEventListener("DOMContentLoaded", boot);
})();
