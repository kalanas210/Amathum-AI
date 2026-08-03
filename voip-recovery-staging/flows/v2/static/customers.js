// customers.js — session-centric view: one row per call, expand for full detail.

const state = {
  sessions: [],          // list of summary objects from /api/sessions
  flows: new Set(),
  paused: false,
  q: "",
  flowFilter: "",
  liveOnly: false,
  expanded: new Set(),   // session ids currently expanded
  details: new Map(),    // sid -> full detail payload (cached)
  streams: new Map(),    // sid -> EventSource (live tails)
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[c]));
}
function tsShort(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    return sameDay
      ? d.toLocaleTimeString(undefined, { hour:"2-digit", minute:"2-digit", second:"2-digit" })
      : d.toLocaleString(undefined, { dateStyle:"short", timeStyle:"short" });
  } catch (_) { return iso.slice(0,19).replace("T"," "); }
}
function durStr(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec/60), s = sec%60;
  return `${m}m ${s}s`;
}

// ----- fetch + render list -----
async function fetchSessions() {
  if (state.paused) return;
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.flowFilter) params.set("flow", state.flowFilter);
  if (state.liveOnly) params.set("live_only", "1");
  params.set("limit", "200");
  try {
    const j = await apiGet("/api/sessions?" + params.toString());
    state.sessions = j.sessions || [];
    (j.available_flows || []).forEach(f => state.flows.add(f));
    refreshFlowFilter();
    renderList();
    document.getElementById("count").textContent = state.sessions.length;
    document.getElementById("server-time").textContent = j.server_time || "—";
    document.getElementById("updated").textContent = new Date().toLocaleTimeString();
  } catch (e) { /* soft-fail */ }
}

function refreshFlowFilter() {
  const sel = document.getElementById("flow-filter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All flows</option>` +
    [...state.flows].sort().map(f => `<option value="${esc(f)}" ${f===cur?"selected":""}>${esc(f)}</option>`).join("");
}

function renderList() {
  const list = document.getElementById("sessions");
  if (!state.sessions.length) {
    list.innerHTML = `<div class="py-8 text-center text-muted-fg text-[12px]">No calls match. Place a test call so the agent can capture info.</div>`;
    return;
  }
  list.innerHTML = state.sessions.map(s => sessionCardHTML(s)).join("");
  list.querySelectorAll("[data-sid]").forEach(row => {
    row.addEventListener("click", (e) => {
      if (e.target.closest(".no-expand")) return; // links inside don't toggle
      toggleExpand(row.dataset.sid);
    });
  });
  // Re-render expanded panels with cached detail
  for (const sid of state.expanded) {
    const det = state.details.get(sid);
    if (det) renderExpanded(sid, det);
  }
  lucide.createIcons();
}

function sessionCardHTML(s) {
  const isExp = state.expanded.has(s.id);
  const fields = s.captured || {};
  const keys = Object.keys(fields);
  const preview = keys.length
    ? keys.slice(0, 4).map(k => `<span class="px-1.5 py-0.5 rounded bg-muted text-muted-fg text-[10px]"><code>${esc(k)}</code>=${esc(String(fields[k]).slice(0,40))}</span>`).join(" ") +
      (keys.length > 4 ? ` <span class="text-[10px] text-muted-fg">+${keys.length-4} more</span>` : "")
    : `<span class="text-[10px] text-muted-fg italic">nothing captured</span>`;
  return `
    <div data-sid="${esc(s.id)}" class="px-4 py-3 hover:bg-bg/30 cursor-pointer">
      <div class="flex items-center gap-3 flex-wrap">
        <i data-lucide="${isExp ? 'chevron-down' : 'chevron-right'}" class="w-3.5 h-3.5 text-muted-fg shrink-0"></i>
        <div class="text-[11px] text-muted-fg font-mono w-28 shrink-0">${esc(tsShort(s.started_at))}</div>
        ${s.is_live
          ? `<span class="flex items-center gap-1 text-[10px] text-emerald-fg shrink-0"><span class="w-2 h-2 rounded-full bg-emerald-fg animate-pulse"></span>LIVE</span>`
          : `<span class="text-[10px] text-muted-fg shrink-0">${durStr(s.duration_sec)}</span>`}
        <div class="text-[12px] font-mono text-fg shrink-0">${esc(s.caller || "—")}</div>
        ${s.flow_id ? `<span class="px-1.5 py-0.5 rounded bg-muted text-muted-fg text-[10px] shrink-0">${esc(s.flow_id)}</span>` : ""}
        <div class="flex-1 min-w-0 flex flex-wrap gap-1.5">${preview}</div>
        <div class="text-[10px] text-muted-fg flex items-center gap-2 shrink-0">
          ${s.transcript_count ? `<span title="Transcript lines"><i data-lucide="message-square" class="w-3 h-3 inline-block -mt-0.5"></i> ${s.transcript_count}</span>` : ""}
          ${s.has_recording ? `<span title="Has recording"><i data-lucide="disc" class="w-3 h-3 inline-block -mt-0.5"></i></span>` : ""}
          <span class="font-mono">${esc(s.id.slice(0,8))}…</span>
        </div>
      </div>
      <div data-expand="${esc(s.id)}" class="${isExp ? "" : "hidden"} mt-3 pl-7"></div>
    </div>`;
}

// ----- expand / collapse -----
async function toggleExpand(sid) {
  if (state.expanded.has(sid)) {
    state.expanded.delete(sid);
    const es = state.streams.get(sid);
    if (es) { es.close(); state.streams.delete(sid); }
    renderList();
    return;
  }
  state.expanded.add(sid);
  renderList();
  // Lazy-load detail
  if (!state.details.has(sid)) {
    try {
      const det = await apiGet(`/api/sessions/${encodeURIComponent(sid)}`);
      state.details.set(sid, det);
    } catch (e) {
      state.details.set(sid, { error: String(e) });
    }
  }
  const det = state.details.get(sid);
  renderExpanded(sid, det);
  // If session is live, attach SSE for transcript tail
  if (det && det.summary && det.summary.is_live) attachLiveStream(sid);
}

function renderExpanded(sid, det) {
  const host = document.querySelector(`[data-expand="${cssEscape(sid)}"]`);
  if (!host) return;
  if (det && det.error) {
    host.innerHTML = `<div class="text-rose-fg text-[12px]">Failed to load: ${esc(det.error)}</div>`;
    return;
  }
  const sum = det.summary;
  const fields = sum.captured || {};
  const keys = Object.keys(fields).sort();
  const fieldsHTML = keys.length
    ? `<table class="text-[12px] w-full"><tbody>${keys.map(k => `
        <tr class="border-b border-border/40">
          <td class="py-1 pr-3 text-muted-fg font-mono w-40 align-top">${esc(k)}</td>
          <td class="py-1 text-fg whitespace-pre-wrap">${esc(fields[k])}</td>
        </tr>`).join("")}</tbody></table>`
    : `<div class="text-[12px] text-muted-fg italic">No fields captured for this call.</div>`;

  const transcripts = (det.transcripts || []).filter(t => t.text && t.text.trim());
  const transcriptHTML = transcripts.length
    ? transcripts.map(t => `
        <div class="${t.role === 'agent' ? '' : 'text-right'}">
          <div class="inline-block px-2.5 py-1.5 rounded-md text-[12px] max-w-[80%] whitespace-pre-wrap text-left ${t.role === 'agent' ? 'bg-muted text-fg' : 'bg-primary/80 text-primary-fg'}">
            <div class="text-[9px] uppercase opacity-60 mb-0.5">${esc(t.role)} · ${esc(tsShort(t.ts))}</div>
            ${esc(t.text)}
          </div>
        </div>`).join("")
    : `<div class="text-[12px] text-muted-fg italic">No transcript captured yet.</div>`;

  const tools = det.tool_calls || [];
  const toolsHTML = tools.length
    ? `<details><summary class="cursor-pointer text-[11px] text-muted-fg hover:text-fg">Tool calls (${tools.length})</summary>
       <ul class="mt-2 text-[11px] font-mono space-y-0.5">${tools.map(t => `<li><span class="text-muted-fg">${esc(tsShort(t.ts))}</span> <span class="text-fg">${esc(t.name)}</span>(${esc(JSON.stringify(t.args))})</li>`).join("")}</ul></details>`
    : "";

  const events = det.events || [];
  const eventsHTML = `<details><summary class="cursor-pointer text-[11px] text-muted-fg hover:text-fg">Raw events (${events.length})</summary>
       <pre class="mt-2 term max-h-72 overflow-y-auto text-[10px]">${esc(events.slice(-200).map(ev => JSON.stringify(ev)).join("\n"))}</pre></details>`;

  const recordingHTML = det.recording_url
    ? `<div class="border border-border rounded-md p-3 bg-bg">
         <div class="text-[11px] text-muted-fg mb-1.5">Recording</div>
         <audio controls class="w-full" src="${esc(det.recording_url)}"></audio>
       </div>`
    : `<div class="text-[11px] text-muted-fg italic">No recording on file. (Enable recording on the flow + wire MixMonitor in dialplan.)</div>`;

  host.innerHTML = `
    <div class="grid lg:grid-cols-2 gap-3 mb-3">
      <div class="border border-border rounded-md p-3 bg-bg">
        <div class="flex items-center justify-between mb-2">
          <div class="text-[11px] text-muted-fg uppercase tracking-wide">Captured info</div>
          <div class="text-[10px] text-muted-fg">${keys.length} field${keys.length === 1 ? "" : "s"}</div>
        </div>
        ${fieldsHTML}
      </div>
      <div class="border border-border rounded-md p-3 bg-bg">
        <div class="flex items-center justify-between mb-2">
          <div class="text-[11px] text-muted-fg uppercase tracking-wide">Call meta</div>
          ${sum.is_live ? `<span class="text-[10px] text-emerald-fg flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-emerald-fg animate-pulse"></span>LIVE</span>` : ""}
        </div>
        <table class="text-[12px] w-full">
          <tbody>
            <tr><td class="py-0.5 pr-3 text-muted-fg w-32">Session</td><td class="font-mono">${esc(sum.id)}</td></tr>
            <tr><td class="py-0.5 pr-3 text-muted-fg">Caller</td><td>${esc(sum.caller || "—")}</td></tr>
            <tr><td class="py-0.5 pr-3 text-muted-fg">Channel</td><td class="font-mono text-[11px]">${esc(sum.channel || "—")}</td></tr>
            <tr><td class="py-0.5 pr-3 text-muted-fg">Flow</td><td>${esc(sum.flow_id || "—")}</td></tr>
            <tr><td class="py-0.5 pr-3 text-muted-fg">Voice</td><td>${esc(sum.voice || "—")}</td></tr>
            <tr><td class="py-0.5 pr-3 text-muted-fg">Started</td><td class="font-mono text-[11px]">${esc(tsShort(sum.started_at))}</td></tr>
            <tr><td class="py-0.5 pr-3 text-muted-fg">Duration</td><td>${esc(durStr(sum.duration_sec))}</td></tr>
            ${sum.end_reason ? `<tr><td class="py-0.5 pr-3 text-muted-fg">Ended</td><td>${esc(sum.end_reason)}</td></tr>` : ""}
          </tbody>
        </table>
      </div>
    </div>
    <div class="border border-border rounded-md p-3 bg-bg mb-3">
      <div class="text-[11px] text-muted-fg uppercase tracking-wide mb-2 flex items-center justify-between">
        <span>Transcript</span>
        ${sum.is_live ? `<span class="text-[10px] text-emerald-fg" data-live-indicator="${esc(sid)}">streaming…</span>` : ""}
      </div>
      <div data-transcript-host="${esc(sid)}" class="space-y-2 max-h-96 overflow-y-auto pr-1">${transcriptHTML}</div>
    </div>
    <div class="mb-3">${recordingHTML}</div>
    <div class="space-y-2">${toolsHTML}${eventsHTML}</div>`;
}

function cssEscape(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c);
}

// ----- live streaming for an expanded session -----
function attachLiveStream(sid) {
  if (state.streams.has(sid)) return;
  const es = new EventSource(`/api/calls/${encodeURIComponent(sid)}/stream`);
  es.onmessage = (ev) => {
    try {
      const j = JSON.parse(ev.data);
      // We only re-render captured + transcript on new events that affect them
      if (j.type === 'transcript') {
        appendTranscript(sid, j);
      } else if (j.type === 'tool_call' && j.name === 'save_customer_info') {
        // patch the cached detail to keep the panel in sync
        const det = state.details.get(sid);
        if (det) {
          det.summary.captured = det.summary.captured || {};
          const a = j.args || {};
          if (a.field) det.summary.captured[a.field] = a.value;
          renderExpanded(sid, det); // re-render this card with updated fields
        }
      } else if (j.type === 'hangup_from_asterisk' || j.type === 'gemini_closed' || j.type === 'ami_hangup') {
        // call ended — close the stream and refresh
        es.close();
        state.streams.delete(sid);
        state.details.delete(sid); // force refetch on next expand
        fetchSessions();
      }
    } catch (_) {}
  };
  es.onerror = () => { es.close(); state.streams.delete(sid); };
  state.streams.set(sid, es);
}

function appendTranscript(sid, ev) {
  const host = document.querySelector(`[data-transcript-host="${cssEscape(sid)}"]`);
  if (!host) return;
  // Don't re-render the whole detail; just append one bubble
  const role = ev.role || "agent";
  const div = document.createElement("div");
  div.className = role === 'agent' ? '' : 'text-right';
  div.innerHTML = `<div class="inline-block px-2.5 py-1.5 rounded-md text-[12px] max-w-[80%] whitespace-pre-wrap text-left ${role === 'agent' ? 'bg-muted text-fg' : 'bg-primary/80 text-primary-fg'}">
    <div class="text-[9px] uppercase opacity-60 mb-0.5">${esc(role)} · ${esc(tsShort(ev.ts))}</div>
    ${esc(ev.text || "")}
  </div>`;
  host.appendChild(div);
  host.scrollTop = host.scrollHeight;
}

// ----- controls -----
document.getElementById("q").addEventListener("input", (e) => { state.q = e.target.value.trim().toLowerCase(); fetchSessions(); });
document.getElementById("flow-filter").addEventListener("change", (e) => { state.flowFilter = e.target.value; fetchSessions(); });
document.getElementById("live-only").addEventListener("change", (e) => { state.liveOnly = e.target.checked; fetchSessions(); });
document.getElementById("btn-pause").addEventListener("click", () => {
  state.paused = !state.paused;
  document.getElementById("pause-label").textContent = state.paused ? "Resume" : "Pause";
  if (!state.paused) fetchSessions();
});

fetchSessions();
setInterval(fetchSessions, 2000);
lucide.createIcons();
