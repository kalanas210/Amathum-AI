// flows.js — admin /flows page client. ESM module.
//
// Sections:
//   1. State + API helpers
//   2. List rendering (left rail)
//   3. Editor: tabs + per-tab read/write
//   4. Transfer rules editor (dynamic table)
//   5. Tools editor (checkbox list + per-tool config form)
//   6. Flow diagram (React Flow, lazy-loaded)
//   7. New-flow modal, activate, delete, save
//
// Note: apiGet/apiPost/toast are global helpers from base.html.

const REACT_FLOW_CSS = "https://cdn.jsdelivr.net/npm/reactflow@11/dist/style.css";

// ============ 1. State ============
const state = {
  list: [],          // {id, name, ..., is_active}[]
  activeId: null,
  voices: [],
  toolCatalog: [],
  editing: null,     // full flow object being edited
  selectedNodeId: null,
  reactFlowReady: false,
  reactFlowRoot: null,
};

async function apiPut(url, body) {
  const r = await fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}
async function apiDelete(url) {
  const r = await fetch(url, { method: "DELETE" });
  return r.json();
}

// ============ 2. List rendering ============
async function reloadList() {
  const data = await apiGet("/api/flows");
  state.list = data.flows || [];
  state.activeId = data.active_id;
  renderList();
  populateCloneDropdown();
}

function renderList() {
  const el = document.getElementById("flow-list");
  if (!state.list.length) {
    el.innerHTML = `<div class="text-[12px] text-muted-fg">No flows yet. Click <b>New flow</b>.</div>`;
    return;
  }
  el.innerHTML = state.list.map((f) => {
    const isActive = f.is_active;
    return `
      <div class="border border-border rounded-md bg-card p-3 hover:border-border-strong transition-colors" data-flow-id="${f.id}">
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <div class="flex items-center gap-1.5">
              <h4 class="text-sm font-medium text-fg truncate">${escape(f.name)}</h4>
              ${isActive ? `<span class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-emerald-bg text-emerald-fg shrink-0">Active</span>` : ""}
              ${f.is_preset ? `<span class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-muted text-muted-fg shrink-0">Preset</span>` : ""}
            </div>
            <div class="text-[11px] text-muted-fg mt-0.5 line-clamp-2">${escape(f.description || "—")}</div>
            <div class="text-[10px] text-muted-fg mt-1 font-mono">${f.id} · voice: ${f.voice || "?"} · ${f.transfer_rules_count} rule(s) · ${f.tools_count} tool(s)</div>
          </div>
        </div>
        <div class="flex gap-1.5 mt-2 flex-wrap">
          ${isActive ? "" : `<button data-action="activate" class="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-muted">Activate</button>`}
          <button data-action="edit" class="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-muted">Edit</button>
          <button data-action="clone" class="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-muted">Clone</button>
          ${!f.is_preset && !isActive ? `<button data-action="delete" class="text-[11px] px-2 py-0.5 rounded border border-border text-rose-fg hover:bg-muted">Delete</button>` : ""}
        </div>
      </div>`;
  }).join("");
  el.querySelectorAll("[data-flow-id]").forEach((card) => {
    const id = card.dataset.flowId;
    card.querySelectorAll("[data-action]").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const action = btn.dataset.action;
        if (action === "edit") await openEditor(id);
        else if (action === "activate") await activateFlow(id);
        else if (action === "clone") await cloneFlow(id);
        else if (action === "delete") await deleteFlow(id);
      });
    });
  });
  lucide.createIcons();
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[c]));
}
const esc = escape;  // alias so newer code that uses esc() works too

// ============ 3. Editor ============
async function openEditor(id) {
  const flow = await apiGet(`/api/flows/${id}`);
  state.editing = flow;
  state.selectedNodeId = null;
  document.getElementById("editor-empty").classList.add("hidden");
  document.getElementById("editor").classList.remove("hidden");

  // Header
  document.getElementById("ed-name").textContent = flow.name || flow.id;
  document.getElementById("ed-id").textContent = flow.id;
  document.getElementById("ed-active-badge").classList.toggle("hidden", flow.id !== state.activeId);
  document.getElementById("ed-preset-badge").classList.toggle("hidden", !flow.is_preset);

  // Tabs
  switchTab("identity");

  // Populate identity
  setVal("f-name", flow.name);
  setVal("f-desc", flow.description);
  setVal("f-model", flow.model);
  setVal("f-lang", flow.language_hint);
  setVal("f-tmo", flow.escalation_timeout_sec);
  setVal("f-testnum", flow.test_mode_number);
  document.getElementById("f-testmode").checked = !!flow.test_mode;
  populateVoices(flow.voice);

  // Prompts
  setVal("f-greeting", flow.greeting_trigger);
  setVal("f-retry-greeting", flow.retry_greeting_trigger);
  setVal("f-system-prompt", flow.system_prompt);
  setVal("f-custom", flow.custom_instructions);

  // Transfer + Tools
  renderRules(flow.transfer_rules || []);
  renderTools(flow.tools_enabled || [], flow.tools_config || {});

  // Disable editing for presets
  const presetLock = flow.is_preset;
  document.querySelectorAll("#editor input,#editor textarea,#editor select,#editor button.tab-btn").forEach((el) => {
    // Tab buttons stay enabled; everything else readonly for presets
    if (el.classList.contains("tab-btn")) return;
    el.disabled = presetLock;
  });
  document.getElementById("btn-save").disabled = presetLock;
  document.getElementById("btn-save-activate").disabled = false; // can still activate a preset
}

function setVal(id, v) {
  const el = document.getElementById(id);
  if (el) el.value = v ?? "";
}
function getVal(id) {
  return document.getElementById(id)?.value ?? "";
}

function populateVoices(selected) {
  const sel = document.getElementById("f-voice");
  sel.innerHTML = state.voices.map((v) => `<option ${v === selected ? "selected" : ""}>${escape(v)}</option>`).join("");
}

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("border-border", on);
    b.classList.toggle("bg-card", on);
    b.classList.toggle("text-fg", on);
    b.classList.toggle("text-muted-fg", !on);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("hidden", p.dataset.tabPanel !== name);
  });
  if (name === "diagram") ensureReactFlow();
}

document.getElementById("tab-nav").addEventListener("click", (e) => {
  const t = e.target.closest(".tab-btn");
  if (t) switchTab(t.dataset.tab);
});

// ============ 4. Transfer rules editor ============
function renderRules(rules) {
  const tbody = document.getElementById("rules-tbody");
  tbody.innerHTML = rules.map((r, i) => ruleRowHTML(r, i)).join("");
  attachRuleHandlers();
}

function ruleRowHTML(r, i) {
  const isDefault = r.category === "default";
  return `
    <tr data-rule-i="${i}">
      <td class="py-1.5 px-2"><input data-k="category" type="text" value="${escape(r.category)}" ${isDefault ? "readonly" : ""} class="w-full px-2 py-1 rounded border border-border bg-card text-sm ${isDefault ? "text-muted-fg" : ""}"></td>
      <td class="py-1.5 px-2"><input data-k="manager_number" type="text" pattern="[0-9+]{4,18}" value="${escape(r.manager_number)}" class="w-40 px-2 py-1 rounded border border-border bg-card text-sm font-mono"></td>
      <td class="py-1.5 px-2"><input data-k="description" type="text" value="${escape(r.description || "")}" class="w-full px-2 py-1 rounded border border-border bg-card text-sm"></td>
      <td class="py-1.5 px-2">${isDefault ? "" : `<button data-action="del-rule" class="text-rose-fg" title="Delete row"><i data-lucide="x" class="w-4 h-4"></i></button>`}</td>
    </tr>`;
}

function attachRuleHandlers() {
  document.querySelectorAll("[data-action=del-rule]").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.target.closest("tr").remove();
    });
  });
  lucide.createIcons();
}

document.getElementById("btn-add-rule").addEventListener("click", () => {
  const tbody = document.getElementById("rules-tbody");
  const idx = tbody.querySelectorAll("tr").length;
  tbody.insertAdjacentHTML("beforeend", ruleRowHTML({ category: "", manager_number: "", description: "" }, idx));
  attachRuleHandlers();
});

function collectRules() {
  return Array.from(document.querySelectorAll("#rules-tbody tr")).map((tr) => ({
    category: tr.querySelector("[data-k=category]").value.trim(),
    manager_number: tr.querySelector("[data-k=manager_number]").value.trim(),
    description: tr.querySelector("[data-k=description]").value.trim(),
  })).filter((r) => r.category && r.manager_number);
}

// ============ 5. Tools editor ============
function renderTools(enabled, config) {
  const list = document.getElementById("tools-list");
  const enabledSet = new Set(enabled);
  list.innerHTML = state.toolCatalog.map((t) => {
    const on = enabledSet.has(t.id);
    const cfg = config[t.id] || {};
    const schema = t.config_schema || {};
    const fieldEls = Object.entries(schema).map(([key, spec]) => toolConfigFieldHTML(t.id, key, spec, cfg[key])).join("");
    return `
      <div class="border border-border rounded-md p-3 bg-card">
        <label class="flex items-start gap-2 cursor-pointer">
          <input type="checkbox" data-tool-id="${t.id}" ${on ? "checked" : ""} class="mt-0.5">
          <div class="min-w-0 flex-1">
            <div class="text-sm font-medium text-fg">${escape(t.name)} <span class="text-[11px] font-normal text-muted-fg font-mono">${t.id}</span></div>
            <div class="text-[12px] text-muted-fg mt-0.5">${escape(t.description)}</div>
            ${fieldEls ? `<div class="mt-2 space-y-2 ${on ? "" : "opacity-50 pointer-events-none"}" data-tool-cfg="${t.id}">${fieldEls}</div>` : ""}
          </div>
        </label>
      </div>`;
  }).join("");
  list.querySelectorAll("[data-tool-id]").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const cfgEl = list.querySelector(`[data-tool-cfg="${cb.dataset.toolId}"]`);
      if (cfgEl) cfgEl.classList.toggle("opacity-50", !cb.checked);
      if (cfgEl) cfgEl.classList.toggle("pointer-events-none", !cb.checked);
    });
  });
}

function toolConfigFieldHTML(toolId, key, spec, value) {
  if (spec.type === "string-list") {
    const txt = Array.isArray(value) ? value.join("\n") : (spec.default || []).join("\n");
    return `
      <label class="block">
        <span class="block text-[11px] text-muted-fg mb-1">${escape(spec.label || key)} <span class="opacity-60">${escape(spec.help || "")}</span></span>
        <textarea data-cfg-key="${key}" data-cfg-type="string-list" rows="4" class="w-full px-2 py-1 rounded border border-border bg-card text-sm font-mono">${escape(txt)}</textarea>
      </label>`;
  }
  // Fallback: string input
  return `
    <label class="block">
      <span class="block text-[11px] text-muted-fg mb-1">${escape(spec.label || key)}</span>
      <input data-cfg-key="${key}" data-cfg-type="string" type="text" value="${escape(value ?? spec.default ?? "")}" class="w-full px-2 py-1 rounded border border-border bg-card text-sm">
    </label>`;
}

function collectTools() {
  const enabled = [];
  const config = {};
  document.querySelectorAll("#tools-list [data-tool-id]").forEach((cb) => {
    if (!cb.checked) return;
    const tid = cb.dataset.toolId;
    enabled.push(tid);
    const cfgEl = cb.closest(".border").querySelector(`[data-tool-cfg="${tid}"]`);
    if (!cfgEl) return;
    const cfg = {};
    cfgEl.querySelectorAll("[data-cfg-key]").forEach((el) => {
      const key = el.dataset.cfgKey;
      if (el.dataset.cfgType === "string-list") {
        cfg[key] = el.value.split("\n").map((s) => s.trim()).filter(Boolean);
      } else {
        cfg[key] = el.value;
      }
    });
    if (Object.keys(cfg).length) config[tid] = cfg;
  });
  return { enabled, config };
}

// ============ 6. Flow diagram (React Flow, lazy) ============
async function ensureReactFlow() {
  if (state.reactFlowReady) {
    renderFlowDiagram();
    return;
  }
  // Inject CSS
  if (!document.querySelector(`link[href="${REACT_FLOW_CSS}"]`)) {
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = REACT_FLOW_CSS;
    document.head.appendChild(l);
  }
  try {
    const React = (await import("https://esm.sh/react@18")).default;
    const ReactDOM = await import("https://esm.sh/react-dom@18/client");
    const RF = await import("https://esm.sh/reactflow@11");
    window.__React = React;
    window.__ReactDOM = ReactDOM;
    window.__ReactFlow = RF;
    state.reactFlowReady = true;
    renderFlowDiagram();
  } catch (e) {
    document.getElementById("flow-canvas").innerHTML =
      `<div class="p-6 text-rose-fg text-[12px]">Could not load React Flow from CDN (${escape(String(e))}). The diagram editor needs internet access. Other tabs still work; the system prompt drives behaviour directly.</div>`;
  }
}

function renderFlowDiagram() {
  if (!state.editing) return;
  const React = window.__React;
  const { createRoot } = window.__ReactDOM;
  const { default: ReactFlow, Background, Controls, MiniMap, applyNodeChanges, applyEdgeChanges, addEdge } = window.__ReactFlow;
  const container = document.getElementById("flow-canvas");
  if (!state.reactFlowRoot) {
    state.reactFlowRoot = createRoot(container);
  }

  const flow = state.editing.flow || { nodes: [], edges: [] };

  // We use plain (default) React Flow nodes; the visible label is data.label.
  function Wrapper() {
    const [nodes, setNodes] = React.useState(
      (flow.nodes || []).map((n) => ({
        id: n.id,
        position: n.position || { x: 100, y: 100 },
        data: { label: nodeLabel(n) },
        style: nodeStyle(n.type),
        _meta: n, // original
      }))
    );
    const [edges, setEdges] = React.useState(flow.edges || []);
    const onNodesChange = React.useCallback((c) => setNodes((nds) => applyNodeChanges(c, nds)), []);
    const onEdgesChange = React.useCallback((c) => setEdges((eds) => applyEdgeChanges(c, eds)), []);
    const onConnect = React.useCallback((c) => setEdges((eds) => addEdge({ ...c, id: `e${Date.now()}` }, eds)), []);
    const onSelectionChange = React.useCallback((sel) => {
      const sn = sel.nodes && sel.nodes[0];
      state.selectedNodeId = sn?.id || null;
      const meta = sn && nodes.find((n) => n.id === sn.id)?._meta;
      renderInspector(meta);
    }, [nodes]);

    // Expose for the toolbox buttons + save
    window.__flowGetState = () => ({
      nodes: nodes.map((n) => ({
        ...(n._meta || {}),
        id: n.id,
        type: n._meta?.type,
        position: n.position,
        data: n._meta?.data || { label: n.data.label },
      })),
      edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, label: e.label })),
      viewport: flow.viewport,
    });
    window.__flowAddNode = (type) => {
      const id = `${type}-${Date.now().toString(36)}`;
      const meta = { id, type, position: { x: 200 + Math.random() * 200, y: 200 + Math.random() * 100 }, data: { label: type } };
      setNodes((nds) => nds.concat({ id, position: meta.position, data: { label: nodeLabel(meta) }, style: nodeStyle(type), _meta: meta }));
    };
    window.__flowDeleteSelected = () => {
      if (!state.selectedNodeId) return;
      setNodes((nds) => nds.filter((n) => n.id !== state.selectedNodeId));
      setEdges((eds) => eds.filter((e) => e.source !== state.selectedNodeId && e.target !== state.selectedNodeId));
      state.selectedNodeId = null;
      renderInspector(null);
    };
    window.__flowUpdateNodeData = (id, patch) => {
      setNodes((nds) => nds.map((n) => {
        if (n.id !== id) return n;
        const newMeta = { ...n._meta, data: { ...(n._meta?.data || {}), ...patch } };
        return { ...n, _meta: newMeta, data: { label: nodeLabel(newMeta) } };
      }));
    };

    return React.createElement(
      "div", { style: { height: "480px" } },
      React.createElement(
        ReactFlow,
        {
          nodes, edges, onNodesChange, onEdgesChange, onConnect,
          onSelectionChange, fitView: true,
        },
        React.createElement(Background, { gap: 16 }),
        React.createElement(Controls, null),
        React.createElement(MiniMap, { pannable: true })
      )
    );
  }

  state.reactFlowRoot.render(React.createElement(Wrapper));
}

function nodeLabel(node) {
  const t = node.type || "?";
  const d = node.data || {};
  if (t === "start") return `Start\n${d.greeting_text || ""}`.slice(0, 80);
  if (t === "intent") return `Intent: ${d.label || ""}`;
  if (t === "response") return `Say: ${(d.message_text || "").slice(0, 40)}`;
  if (t === "tool") return `Tool: ${d.tool_id || "?"}`;
  if (t === "transfer") return `Transfer → ${d.category || "default"}`;
  if (t === "end") return `End: ${(d.farewell_text || "").slice(0, 40)}`;
  return d.label || t;
}

function nodeStyle(type) {
  const styles = {
    start:    { background: "#16a34a", color: "white" },
    intent:   { background: "#0ea5e9", color: "white" },
    response: { background: "#6366f1", color: "white" },
    tool:     { background: "#f59e0b", color: "white" },
    transfer: { background: "#a855f7", color: "white" },
    end:      { background: "#dc2626", color: "white" },
  };
  return { ...(styles[type] || {}), padding: 8, borderRadius: 8, fontSize: 11, width: 160, whiteSpace: "pre-wrap" };
}

function renderInspector(meta) {
  const ins = document.getElementById("flow-inspector");
  if (!meta) { ins.innerHTML = `<div class="text-muted-fg">Select a node to edit its config.</div>`; return; }
  const t = meta.type;
  const d = meta.data || {};
  const rules = collectRules();
  const tools = collectTools().enabled;
  let html = `<div class="space-y-2">
    <div class="font-mono text-[11px] text-muted-fg">${escape(meta.id)} · type=${escape(t)}</div>`;
  if (t === "start") {
    html += inspField("greeting_text", d.greeting_text, "textarea", "Greeting text");
  } else if (t === "intent") {
    html += inspField("label", d.label, "text", "Label");
    html += inspField("description", d.description, "textarea", "Description (when to take this branch)");
  } else if (t === "response") {
    html += inspField("message_text", d.message_text, "textarea", "Message text");
  } else if (t === "tool") {
    html += inspSelect("tool_id", d.tool_id, tools.length ? tools : state.toolCatalog.map((x) => x.id), "Tool");
    html += inspField("arg_template", d.arg_template, "textarea", "Argument template / instructions");
  } else if (t === "transfer") {
    html += inspSelect("category", d.category || "default", rules.map((r) => r.category), "Transfer category");
  } else if (t === "end") {
    html += inspField("farewell_text", d.farewell_text, "textarea", "Farewell text");
  }
  html += `</div>`;
  ins.innerHTML = html;
  ins.querySelectorAll("[data-inspk]").forEach((el) => {
    el.addEventListener("input", () => {
      const patch = { [el.dataset.inspk]: el.value };
      window.__flowUpdateNodeData(state.selectedNodeId, patch);
    });
  });
}

function inspField(key, val, kind, label) {
  if (kind === "textarea") {
    return `<label class="block">
      <span class="block text-[11px] text-muted-fg mb-1">${escape(label)}</span>
      <textarea data-inspk="${key}" rows="3" class="w-full px-2 py-1 rounded border border-border bg-card text-sm">${escape(val || "")}</textarea>
    </label>`;
  }
  return `<label class="block">
    <span class="block text-[11px] text-muted-fg mb-1">${escape(label)}</span>
    <input data-inspk="${key}" type="text" value="${escape(val || "")}" class="w-full px-2 py-1 rounded border border-border bg-card text-sm">
  </label>`;
}
function inspSelect(key, val, opts, label) {
  return `<label class="block">
    <span class="block text-[11px] text-muted-fg mb-1">${escape(label)}</span>
    <select data-inspk="${key}" class="w-full px-2 py-1 rounded border border-border bg-card text-sm">
      ${opts.map((o) => `<option ${o === val ? "selected" : ""}>${escape(o)}</option>`).join("")}
    </select>
  </label>`;
}

document.getElementById("btn-flow-add").addEventListener("click", () => {
  const t = document.getElementById("flow-add-type").value;
  if (!window.__flowAddNode) { toast("Open the Flow diagram tab first.", "error"); return; }
  window.__flowAddNode(t);
});
document.getElementById("btn-flow-delete").addEventListener("click", () => {
  if (window.__flowDeleteSelected) window.__flowDeleteSelected();
});

// ============ 7. Save / activate / new / clone / delete ============
function collectCurrent() {
  if (!state.editing) return null;
  const rules = collectRules();
  const tools = collectTools();
  const flowGraph = window.__flowGetState ? window.__flowGetState() : (state.editing.flow || {});
  return {
    ...state.editing,
    name: getVal("f-name"),
    description: getVal("f-desc"),
    voice: getVal("f-voice"),
    model: getVal("f-model"),
    language_hint: getVal("f-lang"),
    escalation_timeout_sec: parseInt(getVal("f-tmo") || "60", 10),
    test_mode: document.getElementById("f-testmode").checked,
    test_mode_number: getVal("f-testnum"),
    greeting_trigger: getVal("f-greeting"),
    retry_greeting_trigger: getVal("f-retry-greeting"),
    system_prompt: getVal("f-system-prompt"),
    custom_instructions: getVal("f-custom"),
    transfer_rules: rules,
    tools_enabled: tools.enabled,
    tools_config: tools.config,
    flow: flowGraph,
  };
}

async function saveFlow(thenActivate) {
  const data = collectCurrent();
  if (!data) return;
  if (state.editing.is_preset) {
    if (!thenActivate) { toast("Presets can't be edited — clone first.", "error"); return; }
    // For activating presets, just call activate
    return activateFlow(data.id);
  }
  const res = await apiPut(`/api/flows/${data.id}`, data);
  if (res.error) { toast(`Save failed: ${res.error}${res.details ? " — " + res.details.join("; ") : ""}`, "error"); return; }
  toast("Saved.");
  if (thenActivate) await activateFlow(data.id);
  await reloadList();
  await openEditor(data.id);
}

async function activateFlow(id) {
  // Guards both entry points — the Activate button on a card and "Save & activate"
  // — because either one puts this persona live on the production DID.
  if (!confirm(`Activate flow "${id}"?\n\nIt goes live on the production DID immediately: every incoming call from now on is answered by this persona.`)) return;
  const res = await apiPost(`/api/flows/${id}/activate`, {});
  if (res.error) { toast("Activate failed: " + res.error, "error"); return; }
  toast(`Activated ${id}.`);
  await reloadList();
  if (state.editing?.id === id) document.getElementById("ed-active-badge").classList.remove("hidden");
  // Also refresh other active-badges in cards
}

async function cloneFlow(id) {
  const name = prompt("New flow name?", `Copy of ${state.list.find((f) => f.id === id)?.name || id}`);
  if (!name) return;
  const res = await apiPost(`/api/flows/${id}/clone`, { name });
  if (res.error) { toast("Clone failed: " + res.error, "error"); return; }
  toast(`Cloned as ${res.id}.`);
  await reloadList();
  await openEditor(res.id);
}

async function deleteFlow(id) {
  if (!confirm(`Delete flow "${id}"? This cannot be undone.`)) return;
  const res = await apiDelete(`/api/flows/${id}`);
  if (res.error) { toast("Delete failed: " + res.error, "error"); return; }
  toast(`Deleted ${id}.`);
  state.editing = null;
  document.getElementById("editor").classList.add("hidden");
  document.getElementById("editor-empty").classList.remove("hidden");
  await reloadList();
}

document.getElementById("btn-save").addEventListener("click", () => saveFlow(false));
document.getElementById("btn-save-activate").addEventListener("click", () => saveFlow(true));
document.getElementById("btn-discard").addEventListener("click", async () => {
  if (state.editing) await openEditor(state.editing.id);
});

// New-flow modal
function populateCloneDropdown() {
  const sel = document.getElementById("nf-clone");
  sel.innerHTML = `<option value="">Blank (minimal flow)</option>` +
    state.list.map((f) => `<option value="${f.id}">Clone: ${escape(f.name)}</option>`).join("");
}
document.getElementById("btn-new").addEventListener("click", () => {
  document.getElementById("nf-name").value = "";
  document.getElementById("nf-desc").value = "";
  document.getElementById("nf-clone").value = "";
  document.getElementById("modal-new").classList.remove("hidden");
});
document.getElementById("nf-cancel").addEventListener("click", () => {
  document.getElementById("modal-new").classList.add("hidden");
});
document.getElementById("nf-create").addEventListener("click", async () => {
  const name = document.getElementById("nf-name").value.trim();
  if (!name) { toast("Name required.", "error"); return; }
  const body = {
    name,
    description: document.getElementById("nf-desc").value.trim(),
    clone_from: document.getElementById("nf-clone").value || undefined,
  };
  const res = await apiPost("/api/flows", body);
  if (res.error) { toast("Create failed: " + (res.details?.join("; ") || res.error), "error"); return; }
  document.getElementById("modal-new").classList.add("hidden");
  toast(`Created ${res.id}.`);
  await reloadList();
  await openEditor(res.id);
});

// ============ v2 additions: schedule (working hours + recording), playground,
// generate-from-idea, voice sample, edit-a-copy. =============================

const DAYS = [
  { idx: "0", name: "Sun" }, { idx: "1", name: "Mon" }, { idx: "2", name: "Tue" },
  { idx: "3", name: "Wed" }, { idx: "4", name: "Thu" }, { idx: "5", name: "Fri" },
  { idx: "6", name: "Sat" },
];

function renderWorkingHours(flow) {
  const wh = flow.working_hours || {};
  document.getElementById("f-rec-enabled").checked = !!flow.record_calls;
  document.getElementById("f-wh-enabled").checked = !!wh.enabled;
  document.getElementById("f-wh-tz").value = wh.timezone || "Asia/Colombo";
  document.getElementById("f-wh-action").value = wh.out_of_hours_action || "greet";
  document.getElementById("f-wh-greeting").value = wh.out_of_hours_greeting || "";
  document.getElementById("f-wh-cat").value = wh.out_of_hours_transfer_category || "default";
  document.getElementById("f-wh-hangup").value = wh.out_of_hours_hangup_message || "";
  const sched = wh.schedule || {};
  const tb = document.getElementById("wh-days");
  tb.innerHTML = DAYS.map(d => `
    <tr>
      <td class="pr-3 py-1 text-muted-fg w-12">${d.name}</td>
      <td><input data-wh-day="${d.idx}" type="text" placeholder="(closed)" value="${esc(sched[d.idx] || "")}"
                 class="px-2 py-1 rounded border border-border bg-card text-[12px] font-mono w-72"></td>
    </tr>`).join("");
  const cfgWrap = document.getElementById("wh-config");
  const apply = () => {
    const on = document.getElementById("f-wh-enabled").checked;
    cfgWrap.classList.toggle("opacity-50", !on);
    cfgWrap.classList.toggle("pointer-events-none", !on);
  };
  document.getElementById("f-wh-enabled").onchange = apply;
  apply();
}

function collectWorkingHours() {
  const schedule = {};
  document.querySelectorAll("[data-wh-day]").forEach(el => {
    schedule[el.dataset.whDay] = el.value.trim();
  });
  return {
    enabled: document.getElementById("f-wh-enabled").checked,
    timezone: document.getElementById("f-wh-tz").value.trim() || "Asia/Colombo",
    schedule,
    out_of_hours_action: document.getElementById("f-wh-action").value,
    out_of_hours_greeting: document.getElementById("f-wh-greeting").value,
    out_of_hours_transfer_category: document.getElementById("f-wh-cat").value.trim() || "default",
    out_of_hours_hangup_message: document.getElementById("f-wh-hangup").value,
  };
}

// Hook into openEditor + collectCurrent
const _origOpenEditor = openEditor;
openEditor = async function(id) {
  await _origOpenEditor(id);
  renderWorkingHours(state.editing || {});
};
const _origCollect = collectCurrent;
collectCurrent = function() {
  const base = _origCollect();
  if (!base) return null;
  base.working_hours = collectWorkingHours();
  base.record_calls = document.getElementById("f-rec-enabled").checked;
  return base;
};

// Voice sample preview — reuses existing /api/ai-agent/voice-test endpoint
document.getElementById("btn-voice-sample").addEventListener("click", async () => {
  const voice = getVal("f-voice");
  if (!voice) return;
  const btn = document.getElementById("btn-voice-sample");
  btn.disabled = true;
  try {
    const r = await fetch("/api/ai-agent/voice-test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice, text: `Hello, this is the ${voice} voice. I'll be your agent today.` }),
    });
    const j = await r.json();
    if (j.url) {
      const audio = document.getElementById("voice-sample-audio");
      audio.src = j.url;
      audio.play().catch(() => toast("Audio play blocked — click again or check browser audio settings", "error"));
    } else {
      toast(j.error || "Voice sample failed", "error");
    }
  } catch (e) {
    toast("Voice sample failed: " + e, "error");
  } finally {
    btn.disabled = false;
  }
});

// Playground — chat with the saved flow via Gemini 2.5 Pro
const pg = { messages: [] };
function pgRender() {
  const log = document.getElementById("pg-log");
  log.innerHTML = pg.messages.map(m => `
    <div class="${m.role === 'user' ? 'text-right' : ''}">
      <div class="inline-block px-3 py-1.5 rounded-md ${m.role === 'user' ? 'bg-primary text-primary-fg' : 'bg-muted text-fg'} max-w-[80%] whitespace-pre-wrap text-left">${esc(m.text)}</div>
    </div>`).join("") || `<div class="text-muted-fg text-[12px]">Type a message below to start the conversation.</div>`;
  log.scrollTop = log.scrollHeight;
}
async function pgSend() {
  if (!state.editing) { toast("Open a flow first.", "error"); return; }
  const input = document.getElementById("pg-input");
  const text = input.value.trim();
  if (!text) return;
  pg.messages.push({ role: "user", text });
  input.value = "";
  pgRender();
  const r = await fetch(`/api/flows/${state.editing.id}/playground`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: pg.messages.map(m => ({ role: m.role, text: m.text })) }),
  });
  const j = await r.json();
  if (j.error) { toast("Playground: " + j.error, "error"); pg.messages.push({ role: "model", text: "(error: " + j.error + ")" }); }
  else pg.messages.push({ role: "model", text: j.reply || "(empty)" });
  pgRender();
}
document.getElementById("pg-send").addEventListener("click", pgSend);
document.getElementById("pg-input").addEventListener("keydown", (e) => { if (e.key === "Enter") pgSend(); });
document.getElementById("pg-reset").addEventListener("click", () => { pg.messages = []; pgRender(); });

// Edit a copy: when the user clicks Edit on a preset card, auto-clone first.
// We override the list-render click handlers by intercepting calls.
const _origOpenEditorForCard = openEditor;
async function openEditorRespectingPresets(id) {
  const meta = state.list.find(f => f.id === id);
  if (meta?.is_preset) {
    if (!confirm(`"${meta.name}" is a preset. I'll create an editable copy first. OK?`)) return;
    const r = await apiPost(`/api/flows/${id}/clone`, { name: `Copy of ${meta.name}` });
    if (r.error) { toast("Clone failed: " + r.error, "error"); return; }
    toast(`Cloned as ${r.id}.`);
    await reloadList();
    return _origOpenEditorForCard(r.id);
  }
  return _origOpenEditorForCard(id);
}
// Re-route renderList's edit click. Easiest: patch the action dispatch by
// listening at the document level for our buttons. Existing handlers stay.
// Note: renderList registers handlers per-card; we override the "edit" path
// by replacing openEditor with the preset-aware variant.
openEditor = openEditorRespectingPresets;

// AI Generate flow from idea
document.getElementById("nf-generate").addEventListener("click", () => {
  document.getElementById("modal-new").classList.add("hidden");
  document.getElementById("modal-gen").classList.remove("hidden");
  document.getElementById("gen-idea").value = "";
  document.getElementById("gen-preview").classList.add("hidden");
  document.getElementById("gen-use").classList.add("hidden");
  document.getElementById("gen-status").textContent = "";
  lucide.createIcons();
});
document.getElementById("gen-cancel").addEventListener("click", () => {
  document.getElementById("modal-gen").classList.add("hidden");
});
let genProposed = null;
document.getElementById("gen-go").addEventListener("click", async () => {
  const idea = document.getElementById("gen-idea").value.trim();
  if (!idea) { toast("Describe what the agent should do.", "error"); return; }
  const btn = document.getElementById("gen-go");
  btn.disabled = true;
  document.getElementById("gen-status").textContent = "Generating with Gemini 2.5 Pro… (10-25s)";
  document.getElementById("gen-preview").classList.add("hidden");
  document.getElementById("gen-use").classList.add("hidden");
  try {
    const r = await fetch("/api/flows/_generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea }),
    });
    const j = await r.json();
    if (j.error) {
      document.getElementById("gen-status").textContent = "Failed: " + j.error;
      return;
    }
    genProposed = j.proposed;
    const prev = document.getElementById("gen-preview");
    prev.classList.remove("hidden");
    prev.innerHTML = `
      <div><b>Name:</b> ${esc(genProposed.name)}</div>
      <div><b>Voice:</b> ${esc(genProposed.voice)} · <b>Language:</b> ${esc(genProposed.language_hint)}</div>
      <div class="mt-2"><b>Description:</b> ${esc(genProposed.description)}</div>
      <div class="mt-2"><b>Transfer rules:</b><ul class="list-disc ml-5 mt-1">${
        genProposed.transfer_rules.map(r => `<li><code>${esc(r.category)}</code> → <code>${esc(r.manager_number)}</code> — ${esc(r.description)}</li>`).join("")
      }</ul></div>
      <div class="mt-2"><b>Tools enabled:</b> ${genProposed.tools_enabled.map(t => `<code>${esc(t)}</code>`).join(", ")}</div>
      <div class="mt-2"><b>Flow diagram:</b> ${genProposed.flow.nodes.length} nodes, ${genProposed.flow.edges.length} edges</div>
      <details class="mt-2"><summary class="cursor-pointer text-muted-fg">System prompt (first 600 chars)</summary><pre class="text-[11px] whitespace-pre-wrap mt-1">${esc(genProposed.system_prompt.slice(0, 600))}${genProposed.system_prompt.length > 600 ? "…" : ""}</pre></details>`;
    document.getElementById("gen-status").textContent = "Ready. Review then click 'Use this flow'.";
    document.getElementById("gen-use").classList.remove("hidden");
  } catch (e) {
    document.getElementById("gen-status").textContent = "Failed: " + e;
  } finally {
    btn.disabled = false;
  }
});
document.getElementById("gen-use").addEventListener("click", async () => {
  if (!genProposed) return;
  // Create with placeholder name+description; then PUT the full content
  const cr = await apiPost("/api/flows", {
    name: genProposed.name,
    description: genProposed.description,
  });
  if (cr.error) { toast("Create failed: " + (cr.details?.join("; ") || cr.error), "error"); return; }
  const merged = { ...genProposed, id: cr.id, is_preset: false };
  const r = await apiPut(`/api/flows/${cr.id}`, merged);
  if (r.error) { toast("Save failed: " + (r.details?.join("; ") || r.error), "error"); return; }
  toast(`Created ${cr.id} from your idea.`);
  document.getElementById("modal-gen").classList.add("hidden");
  await reloadList();
  await openEditor(cr.id);
});

// ============ Gemini health pre-check ============
// Runs once on page load. If Gemini is unreachable / no quota / bad key,
// disable every AI-powered button and show one prominent banner with the
// real error message. Prevents the cryptic "Unexpected token '<'" UX.

const aiState = { healthy: null, model: null, error: null };

async function checkGeminiHealth() {
  try {
    const h = await apiGet("/api/flows/_gemini-health");
    aiState.healthy = !!h.test_call_ok;
    aiState.model = h.model || null;
    aiState.error = h.test_call_error || (h.key_present ? null : "GEMINI_API_KEY missing in /opt/sampath-ai/.env");
  } catch (e) {
    aiState.healthy = false;
    aiState.error = String(e);
  }
  applyAiButtonState();
}

function applyAiButtonState() {
  const buttons = [
    document.getElementById("nf-generate"),
    document.getElementById("btn-regen-prompts"),
    document.getElementById("btn-regen-diagram"),
    document.getElementById("btn-empty-gen"),
  ].filter(Boolean);
  if (aiState.healthy) {
    buttons.forEach(b => { b.disabled = false; b.title = `Using model: ${aiState.model || "gemini-2.5-flash"}`; });
    removeAiBanner();
  } else {
    buttons.forEach(b => { b.disabled = true; b.title = "AI generation disabled — " + (aiState.error || "unknown error"); });
    showAiBanner(aiState.error || "Gemini health check failed");
  }
}

function removeAiBanner() {
  const ex = document.getElementById("ai-banner");
  if (ex) ex.remove();
}

function showAiBanner(msg) {
  removeAiBanner();
  const div = document.createElement("div");
  div.id = "ai-banner";
  div.className = "mb-3 px-4 py-2 rounded-md border border-amber-fg/40 bg-amber-bg/40 text-amber-fg text-[12px] flex items-start gap-2";
  div.innerHTML = `<i data-lucide="alert-triangle" class="w-4 h-4 shrink-0 mt-0.5"></i>
    <div class="flex-1">
      <div class="font-medium text-amber-fg">AI generation is unavailable</div>
      <div class="text-amber-fg/90 mt-0.5">${escape(msg)}</div>
      <div class="text-amber-fg/70 mt-0.5">All AI buttons are disabled until this is resolved. You can still edit prompts and diagrams manually.</div>
    </div>
    <button class="text-amber-fg/80 hover:text-amber-fg" onclick="checkGeminiHealth()" title="Re-check"><i data-lucide="refresh-cw" class="w-3.5 h-3.5"></i></button>`;
  // Insert at the top of the main content area
  const main = document.querySelector("main") || document.body.querySelector(".grid");
  if (main) main.parentNode.insertBefore(div, main);
  else document.body.prepend(div);
  if (window.lucide) lucide.createIcons();
}
window.checkGeminiHealth = checkGeminiHealth;

// ============ v3 additions: in-editor AI prompt regenerator + diagram regenerator + canvas hardening

// Update Flow diagram stats + toggle empty-state vs canvas
function refreshDiagramVisibility() {
  if (!state.editing) return;
  const flow = state.editing.flow || {};
  const n = (flow.nodes || []).length;
  const e = (flow.edges || []).length;
  const stats = document.getElementById("flow-stats");
  if (stats) stats.textContent = n ? `${n} node${n===1?"":"s"} · ${e} edge${e===1?"":"s"}` : "(empty)";
  const empty = document.getElementById("flow-empty");
  const wrap  = document.getElementById("flow-canvas-wrap");
  if (empty && wrap) {
    empty.classList.toggle("hidden", n > 0);
    wrap.classList.toggle("hidden", n === 0);
  }
}

// Force React Flow to re-fit after a tab switch (canvas size may have been 0)
function refitReactFlow() {
  if (!state.reactFlowReady || !window.__React) return;
  // re-render the wrapper; it calls fitView in the constructor
  if (state.editing) renderFlowDiagram();
}

const _origSwitchTab = switchTab;
switchTab = function(name) {
  _origSwitchTab(name);
  if (name === "diagram") {
    refreshDiagramVisibility();
    // give the canvas a tick to lay out, then ensure react flow + refit
    setTimeout(() => { ensureReactFlow(); refitReactFlow(); }, 50);
  }
};

// Refresh visibility every time we open a flow
const _origOpenEditor2 = openEditor;
openEditor = async function(id) {
  await _origOpenEditor2(id);
  refreshDiagramVisibility();
};

// Fit-view button
document.getElementById("btn-flow-fit").addEventListener("click", () => refitReactFlow());

// "Generate with AI" button inside the empty state
document.getElementById("btn-empty-gen").addEventListener("click", () => openRegenDiagramModal());

// =========== AI: Regenerate PROMPTS (Prompts tab) ===========
function openRegenPromptsModal() {
  if (!state.editing) return;
  if (state.editing.is_preset) {
    if (!confirm("Presets can't be edited. Clone first?")) return;
    return cloneFlow(state.editing.id);
  }
  document.getElementById("rp-idea").value = "";
  document.getElementById("rp-preview").classList.add("hidden");
  document.getElementById("rp-apply").classList.add("hidden");
  document.getElementById("rp-status").textContent = "";
  document.getElementById("modal-regen-prompts").classList.remove("hidden");
  lucide.createIcons();
}
document.getElementById("btn-regen-prompts").addEventListener("click", openRegenPromptsModal);
document.getElementById("rp-cancel").addEventListener("click", () => document.getElementById("modal-regen-prompts").classList.add("hidden"));

let rpProposed = null;
document.getElementById("rp-go").addEventListener("click", async () => {
  if (!state.editing) return;
  const idea = document.getElementById("rp-idea").value.trim();
  if (!idea) { toast("Describe what the agent should do.", "error"); return; }
  const btn = document.getElementById("rp-go");
  btn.disabled = true;
  document.getElementById("rp-status").textContent = "Calling Gemini 2.5 Pro… (10-25s)";
  document.getElementById("rp-preview").classList.add("hidden");
  document.getElementById("rp-apply").classList.add("hidden");
  try {
    const r = await fetch(`/api/flows/${state.editing.id}/regenerate-prompts`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea }),
    });
    const j = await r.json();
    if (j.error) { document.getElementById("rp-status").textContent = "Failed: " + j.error; return; }
    rpProposed = j.proposed;
    const prev = document.getElementById("rp-preview");
    prev.classList.remove("hidden");
    prev.innerHTML = `
      <div><div class="text-[10px] uppercase tracking-wide text-muted-fg mb-1">Greeting trigger</div><div class="bg-bg/50 rounded p-2 whitespace-pre-wrap">${esc(rpProposed.greeting_trigger)}</div></div>
      <div><div class="text-[10px] uppercase tracking-wide text-muted-fg mb-1">Retry greeting trigger</div><div class="bg-bg/50 rounded p-2 whitespace-pre-wrap">${esc(rpProposed.retry_greeting_trigger)}</div></div>
      <div><div class="text-[10px] uppercase tracking-wide text-muted-fg mb-1">Custom instructions</div><div class="bg-bg/50 rounded p-2 whitespace-pre-wrap">${esc(rpProposed.custom_instructions || "(empty)")}</div></div>
      <div><div class="text-[10px] uppercase tracking-wide text-muted-fg mb-1">System prompt (${rpProposed.system_prompt.length} chars — first 600 shown)</div><pre class="bg-bg/50 rounded p-2 text-[11px] whitespace-pre-wrap">${esc(rpProposed.system_prompt.slice(0, 600))}${rpProposed.system_prompt.length > 600 ? "…" : ""}</pre></div>`;
    document.getElementById("rp-status").textContent = "Ready. Apply puts these into the editor (still unsaved — review, edit, then click Save).";
    document.getElementById("rp-apply").classList.remove("hidden");
  } catch (e) {
    document.getElementById("rp-status").textContent = "Failed: " + e;
  } finally {
    btn.disabled = false;
  }
});
document.getElementById("rp-apply").addEventListener("click", () => {
  if (!rpProposed) return;
  setVal("f-system-prompt", rpProposed.system_prompt);
  setVal("f-greeting", rpProposed.greeting_trigger);
  setVal("f-retry-greeting", rpProposed.retry_greeting_trigger);
  setVal("f-custom", rpProposed.custom_instructions);
  document.getElementById("modal-regen-prompts").classList.add("hidden");
  toast("Prompts applied to editor. Click Save (or Save & activate) to persist.");
});

// =========== AI: Regenerate DIAGRAM (Flow diagram tab) ===========
function openRegenDiagramModal() {
  if (!state.editing) return;
  if (state.editing.is_preset) {
    if (!confirm("Presets can't be edited. Clone first?")) return;
    return cloneFlow(state.editing.id);
  }
  document.getElementById("rd-extra").value = "";
  document.getElementById("rd-status").textContent = "";
  document.getElementById("modal-regen-diagram").classList.remove("hidden");
  lucide.createIcons();
}
document.getElementById("btn-regen-diagram").addEventListener("click", openRegenDiagramModal);
document.getElementById("rd-cancel").addEventListener("click", () => document.getElementById("modal-regen-diagram").classList.add("hidden"));
document.getElementById("rd-go").addEventListener("click", async () => {
  if (!state.editing) return;
  const extra = document.getElementById("rd-extra").value.trim();
  const btn = document.getElementById("rd-go");
  btn.disabled = true;
  document.getElementById("rd-status").textContent = "Calling Gemini 2.5 Pro… (10-25s)";
  try {
    const r = await fetch(`/api/flows/${state.editing.id}/regenerate-flow`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea: extra }),
    });
    const j = await r.json();
    if (j.error) { document.getElementById("rd-status").textContent = "Failed: " + j.error; return; }
    // Replace the editing flow's diagram in-memory
    state.editing.flow = j.proposed;
    document.getElementById("modal-regen-diagram").classList.add("hidden");
    toast(`Diagram generated: ${j.proposed.nodes.length} nodes, ${j.proposed.edges.length} edges. Click Save to persist.`);
    refreshDiagramVisibility();
    // Force the React Flow root to remount so it picks up the new state.editing.flow
    state.reactFlowRoot = null;
    renderFlowDiagram();
  } catch (e) {
    document.getElementById("rd-status").textContent = "Failed: " + e;
  } finally {
    btn.disabled = false;
  }
});

// ============ boot ============
(async function init() {
  try {
    state.voices = await apiGet("/api/flows/_voices");
    state.toolCatalog = await apiGet("/api/flows/_tool-catalog");
    await reloadList();
    checkGeminiHealth(); // non-blocking; updates banner + button state when done
  } catch (e) {
    document.getElementById("flow-list").innerHTML =
      `<div class="text-rose-fg text-[12px]">Init failed: ${escape(String(e))}</div>`;
  }
})();
