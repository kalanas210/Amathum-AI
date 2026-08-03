"""
automations.py — Naxter Automation Workflow Builder engine ("n8n inside our app").

Sprint-1 foundation, built per the sprint guide (§5 data model, §6 engine,
§7.1–7.5 the five core nodes, §14 API). Self-contained and INDEPENDENT — it has
no dependency on the rest of the Ryzera codebase and writes to a LOCAL ./data dir
so it runs without root.

How it plugs in:
    from automations import init_automations
    init_automations(app)                                  # standalone (no auth)
    init_automations(app, login_required=login_required,   # later: reuse the
                     perm_required=perm_required)           # pbx-monitor auth

The engine (run_workflow + the executors) is pure stdlib and can be imported and
unit-tested without Flask. Flask is only needed for the HTTP API (init_automations).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import secrets
import datetime
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Storage config — env-overridable; local defaults so it runs without root.
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parent
_DATA = Path(os.environ.get("AUTOMATIONS_DATA_DIR", _BASE / "data"))

AUTOMATIONS_DIR = Path(os.environ.get("AUTOMATIONS_DIR", _DATA / "automations"))
RUNS_DIR = Path(os.environ.get("AUTOMATIONS_RUNS_DIR", _DATA / "automations-runs"))
STATE_DIR = Path(os.environ.get("AUTOMATIONS_STATE_DIR", _DATA / "automations-state"))
VERSIONS_DIR = Path(os.environ.get("AUTOMATIONS_VERSIONS_DIR", _DATA / "automations-versions"))

for _d in (AUTOMATIONS_DIR, RUNS_DIR, STATE_DIR, VERSIONS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_WF_BYTES = 256 * 1024          # reject oversized workflows (§5.2)
KEEP_RUNS = 25                      # run-log retention per workflow (§13)
KEEP_VERSIONS = 30                  # version-history retention per workflow (§5.1)
WAIT_CAP_SECONDS = 15              # inline Wait cap until durable wait (§7.3)
HTTP_TIMEOUT = 20                  # HTTP node timeout (§7.2)
HTTP_MAX_BYTES = 1024 * 1024       # HTTP node response cap (§7.2)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_EXPR_RE = re.compile(r"\{\{(.*?)\}\}")

TRIGGER_TYPES = {
    "manualTrigger", "webhookTrigger", "formTrigger",
    "scheduleTrigger",
}


# ---------------------------------------------------------------------------
# Node catalog — drives the palette + the config panel (§8.4). Sprint-1 nodes
# are implemented:True; later-sprint nodes are listed as implemented:False so the
# future canvas can show (greyed) what's coming without the engine running them.
# ---------------------------------------------------------------------------
NODE_CATALOG = [
    {"type": "manualTrigger", "group": "trigger", "name": "Manual / Test",
     "icon": "mouse-pointer-click", "implemented": True, "outputs": 1,
     "description": "Starts the workflow when you press Run.", "params": []},

    {"type": "httpRequest", "group": "action", "name": "HTTP Request",
     "icon": "globe", "implemented": True, "outputs": 1,
     "description": "Call external API", "params": [
         {"key": "method", "label": "Method", "control": "select",
          "options": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
         {"key": "url", "label": "URL", "control": "string", "default": "",
          "help": "Supports expressions, e.g. https://api.x.com/users/{{ $json.id }}"},
         {"key": "headers", "label": "Headers", "control": "json", "default": {}},
         {"key": "bodyType", "label": "Body type", "control": "select",
          "options": ["none", "json", "raw"], "default": "none"},
         {"key": "body", "label": "Body", "control": "text", "default": "",
          "showWhen": {"key": "bodyType", "in": ["json", "raw"]}},
     ]},

    {"type": "wait", "group": "action", "name": "Wait",
     "icon": "timer", "implemented": True, "outputs": 1,
     "description": "Pause before the next step (capped at 15s in v1).", "params": [
         {"key": "amount", "label": "Amount", "control": "number", "default": 1},
         {"key": "unit", "label": "Unit", "control": "select",
          "options": ["seconds", "minutes", "hours"], "default": "seconds"},
     ]},

    {"type": "if", "group": "logic", "name": "IF",
     "icon": "git-branch", "implemented": True, "outputs": 2,
     "description": "Split into True/False",
     "params": [
         {"key": "value1", "label": "Value 1", "control": "string", "default": ""},
         {"key": "operator", "label": "Operator", "control": "select",
          "options": ["equal", "notEqual", "contains", "notContains", "isEmpty",
                      "isNotEmpty", "gt", "lt", "gte", "lte", "regex"], "default": "equal"},
         {"key": "value2", "label": "Value 2", "control": "string", "default": ""},
     ]},

    {"type": "set", "group": "action", "name": "Edit Fields (Set)",
     "icon": "pencil", "implemented": True, "outputs": 1,
     "description": "Modify data",
     "params": [
         {"key": "assignments", "label": "Fields", "control": "fieldlist", "default": []},
         {"key": "keepOnlySet", "label": "Keep only these fields", "control": "bool",
          "default": False},
     ]},

    {"type": "webhookTrigger", "group": "trigger", "name": "Webhook", "icon": "webhook",
     "implemented": True, "outputs": 1,
     "description": "Starts workflow on event", "params": [
         {"key": "method", "label": "Method", "control": "select",
          "options": ["POST", "GET", "PUT", "PATCH", "DELETE"], "default": "POST"},
         {"key": "auth", "label": "Auth", "control": "select",
          "options": ["none", "header secret"], "default": "none"},
         {"key": "responseMode", "label": "Respond", "control": "select",
          "options": ["immediately", "usingRespondNode"], "default": "immediately"},
     ]},
    {"type": "formTrigger", "group": "trigger", "name": "Form", "icon": "clipboard-list",
     "implemented": True, "outputs": 1,
     "description": "Give people a hosted form; submitting it starts the workflow.", "params": [
         {"key": "title", "label": "Form title", "control": "string", "default": "Untitled form"},
         {"key": "description", "label": "Description", "control": "text", "default": ""},
         {"key": "fields", "label": "Fields", "control": "formfields",
          "default": [{"label": "Email", "type": "email", "required": True}]},
         {"key": "submitMessage", "label": "Submit message", "control": "string", "default": "Thanks! We got it."},
     ]},
    {"type": "respondToWebhook", "group": "action", "name": "Respond to Webhook", "icon": "reply",
     "implemented": True, "outputs": 1,
     "description": "Return a custom HTTP response to a webhook caller.", "params": [
         {"key": "statusCode", "label": "Status code", "control": "number", "default": 200},
         {"key": "bodyType", "label": "Body type", "control": "select",
          "options": ["json", "text"], "default": "json"},
         {"key": "body", "label": "Body", "control": "text", "default": '{ "ok": true }'},
     ]},
    # ---- Top-8 nodes promoted to first-class. The visual layer (colour +
    #      icon) lives in static/automations.js → TOP_NODES; the engine runs a
    #      safe pass-through stub for these until their real sprint lands. ----
    {"type": "scheduleTrigger", "group": "trigger", "name": "Schedule Trigger", "icon": "clock",
     "implemented": True, "outputs": 1, "description": "Runs on a timer", "params": [
         {"key": "mode", "label": "Trigger interval", "control": "select",
          "options": ["seconds", "minutes", "hours", "days", "cron"], "default": "minutes"},
         {"key": "every", "label": "Every", "control": "number", "default": 5,
          "showWhen": {"key": "mode", "in": ["seconds", "minutes", "hours", "days"]}},
         {"key": "cron", "label": "Cron expression", "control": "string", "default": "0 * * * *",
          "showWhen": {"key": "mode", "in": ["cron"]}},
     ]},
    {"type": "googleSheets", "group": "action", "name": "Google Sheets", "icon": "file-spreadsheet",
     "implemented": True, "outputs": 1, "description": "Read/Write rows", "params": [
         {"key": "operation", "label": "Operation", "control": "select",
          "options": ["append", "read", "update", "delete"], "default": "append"},
         {"key": "documentId", "label": "Spreadsheet ID / URL", "control": "string", "default": ""},
         {"key": "sheetName", "label": "Sheet name", "control": "string", "default": "Sheet1"},
     ]},
    {"type": "code", "group": "action", "name": "Code", "icon": "terminal",
     "implemented": True, "outputs": 1, "description": "Custom JS/Python", "params": [
         {"key": "language", "label": "Language", "control": "select",
          "options": ["JavaScript", "Python"], "default": "JavaScript"},
         {"key": "code", "label": "Code", "control": "text",
          "default": "// each row is an item; return the items array\nreturn items;"},
     ]},
    {"type": "openAi", "group": "action", "name": "OpenAI", "icon": "sparkles",
     "implemented": True, "outputs": 1, "description": "Generate/Parse text", "params": [
         {"key": "resource", "label": "Resource", "control": "select",
          "options": ["Chat", "Text", "Image"], "default": "Chat"},
         {"key": "model", "label": "Model", "control": "select",
          "options": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"], "default": "gpt-4o-mini"},
         {"key": "prompt", "label": "Prompt", "control": "text", "default": "",
          "help": "Supports expressions, e.g. Summarise {{ $json.text }}"},
     ]},
]
NODE_TYPES = {n["type"]: n for n in NODE_CATALOG}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path, text):
    """Write atomically (.tmp -> rename) so a crash never leaves a half file (§13)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _slugify(name, existing):
    """Generate a valid, unique id from a name (§5 field rules)."""
    base = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:63].rstrip("-") or "workflow"
    if not _ID_RE.match(base):
        base = ("wf-" + base)[:63].rstrip("-")
    cand, i = base, 2
    while cand in existing or not _ID_RE.match(cand):
        cand = f"{base[:60]}-{i}"
        i += 1
    return cand


# ---------------------------------------------------------------------------
# Workflow storage (one JSON file per workflow)
# ---------------------------------------------------------------------------
def _wf_path(wf_id):
    return AUTOMATIONS_DIR / f"{wf_id}.json"


def _wf_load(wf_id):
    p = _wf_path(wf_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _wf_list_ids():
    return sorted(p.stem for p in AUTOMATIONS_DIR.glob("*.json"))


def _wf_save(wf):
    _atomic_write(_wf_path(wf["id"]), json.dumps(wf, indent=2))


def _wf_validate(wf):
    """Return an error string if the workflow is invalid, else None (§5.2)."""
    if not isinstance(wf, dict):
        return "workflow must be an object"
    if len(json.dumps(wf)) > MAX_WF_BYTES:
        return "workflow too large"
    wid = wf.get("id")
    if not (isinstance(wid, str) and _ID_RE.match(wid)):
        return "invalid id"
    nodes = wf.get("nodes")
    if not isinstance(nodes, list):
        return "nodes must be a list"
    seen = set()
    for n in nodes:
        if not isinstance(n, dict):
            return "each node must be an object"
        nid = n.get("id")
        if not nid or nid in seen:
            return f"duplicate or missing node id: {nid!r}"
        seen.add(nid)
        if n.get("type") not in NODE_TYPES:
            return f"unknown node type: {n.get('type')!r}"
    if not isinstance(wf.get("connections", {}), dict):
        return "connections must be an object"
    return None


def create_workflow(name):
    wid = _slugify(name, set(_wf_list_ids()))
    now = _now_iso()
    wf = {
        "id": wid,
        "name": (name or "Untitled workflow")[:80],
        "description": "",
        "active": False,
        "version": 1,
        "nodes": [
            {"id": "trigger", "name": "Manual / Test", "type": "manualTrigger",
             "position": {"x": 80, "y": 160}, "parameters": {}},
        ],
        "connections": {},
        "created_at": now,
        "updated_at": now,
    }
    _wf_save(wf)
    return wf


def _ensure_tokens(wf):
    """Give each webhook/form trigger a stable unguessable token (+ secret if needed)."""
    for n in wf.get("nodes", []) or []:
        t = n.get("type")
        if t in ("webhookTrigger", "formTrigger"):
            p = n.setdefault("parameters", {})
            if not p.get("path"):
                p["path"] = secrets.token_urlsafe(16)
            if t == "webhookTrigger" and p.get("auth") == "header secret" and not p.get("secret"):
                p["secret"] = secrets.token_urlsafe(18)


def _find_by_token(token, node_type):
    """Scan workflows for a trigger node of node_type whose path == token. (Low volume.)"""
    for wid in _wf_list_ids():
        wf = _wf_load(wid)
        if not wf:
            continue
        for n in wf.get("nodes", []) or []:
            if n.get("type") == node_type and (n.get("parameters", {}) or {}).get("path") == token:
                return wf, n
    return None, None


# ---------------------------------------------------------------------------
# Expressions  {{ $json.field.path }}  — safe find-and-replace, NO eval (§6)
# ---------------------------------------------------------------------------
def _walk(obj, dotted):
    cur = obj
    for part in filter(None, str(dotted).split(".")):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _lookup(path, item, ctx=None):
    path = path.strip()
    if path == "$json":
        return item
    if path.startswith("$json."):
        path = path[len("$json."):]
    return _walk(item, path)


def _render(value, item, ctx=None):
    """Replace every {{ ... }} in a string with values from the current item."""
    if not isinstance(value, str) or "{{" not in value:
        return value

    def repl(m):
        r = _lookup(m.group(1), item, ctx)
        if r is None:
            return ""
        return json.dumps(r) if isinstance(r, (dict, list)) else str(r)

    return _EXPR_RE.sub(repl, value)


# ---------------------------------------------------------------------------
# Executors. Signature: (params, items, ctx=None) -> list-of-output-lists.
#   single output -> [out_items]      IF (2 outputs) -> [true_items, false_items]
# ---------------------------------------------------------------------------
def _exec_manual(params, items, ctx=None):
    # Pass the trigger payload (or {}) straight through as the starting data.
    return [list(items) if items else [{}]]


def _exec_set(params, items, ctx=None):
    keep = bool(params.get("keepOnlySet"))
    assigns = params.get("assignments") or []
    out = []
    for it in items:
        base = {} if keep else dict(it)
        for a in assigns:
            name = a.get("name")
            if name:
                base[name] = _render(a.get("value", ""), it, ctx)
        out.append(base)
    return [out]


def _to_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cmp(v1, op, v2):
    if op == "isEmpty":
        return v1 in (None, "", [], {})
    if op == "isNotEmpty":
        return v1 not in (None, "", [], {})
    s1 = "" if v1 is None else str(v1)
    s2 = "" if v2 is None else str(v2)
    if op == "equal":
        return s1 == s2
    if op == "notEqual":
        return s1 != s2
    if op == "contains":
        return s2 in s1
    if op == "notContains":
        return s2 not in s1
    if op == "regex":
        try:
            return re.search(s2, s1) is not None
        except re.error:
            return False
    n1, n2 = _to_number(v1), _to_number(v2)
    if n1 is None or n2 is None:
        return False
    return {"gt": n1 > n2, "lt": n1 < n2, "gte": n1 >= n2, "lte": n1 <= n2}.get(op, False)


def _exec_if(params, items, ctx=None):
    op = params.get("operator", "equal")
    t, f = [], []
    for it in items:
        v1 = _render(params.get("value1", ""), it, ctx)
        v2 = _render(params.get("value2", ""), it, ctx)
        (t if _cmp(v1, op, v2) else f).append(it)
    return [t, f]


def _clamp_wait_seconds(params):
    amt = _to_number(params.get("amount", 0)) or 0
    secs = amt * {"seconds": 1, "minutes": 60, "hours": 3600}.get(params.get("unit", "seconds"), 1)
    return max(0, min(WAIT_CAP_SECONDS, secs))


def _exec_wait(params, items, ctx=None):
    time.sleep(_clamp_wait_seconds(params))
    return [list(items)]


def _exec_http(params, items, ctx=None):
    method = (params.get("method") or "GET").upper()
    body_type = params.get("bodyType", "none")
    raw_headers = params.get("headers") or {}
    out = []
    for it in items:
        result = dict(it)
        url = _render(params.get("url", ""), it, ctx)
        if not url:
            result["_http"] = {"error": "no url"}
            out.append(result)
            continue
        headers = {}
        if isinstance(raw_headers, dict):
            for k, v in raw_headers.items():
                headers[k] = _render(v, it, ctx) if isinstance(v, str) else v
        data = None
        if body_type in ("json", "raw"):
            body = _render(params.get("body", ""), it, ctx)
            data = body.encode("utf-8") if isinstance(body, str) else body
            if body_type == "json":
                headers.setdefault("Content-Type", "application/json")
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                text = resp.read(HTTP_MAX_BYTES).decode("utf-8", "replace")
                parsed = text
                if "json" in resp.headers.get("Content-Type", "").lower():
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        parsed = text
                result["_http"] = {"statusCode": resp.status,
                                   "headers": dict(resp.headers), "body": parsed}
        except urllib.error.HTTPError as e:
            try:
                body = e.read(HTTP_MAX_BYTES).decode("utf-8", "replace")
            except Exception:
                body = ""
            result["_http"] = {"statusCode": e.code,
                               "headers": dict(getattr(e, "headers", {}) or {}), "body": body}
        except Exception as e:                       # network failure -> soft error (§6)
            result["_http"] = {"error": str(e)}
        out.append(result)
    return [out]


def _exec_respond(params, items, ctx=None):
    # Pass items through unchanged; the engine captures the HTTP response separately.
    return [list(items)]


def _exec_passthrough(params, items, ctx=None):
    # Stub for Top-8 nodes whose real engine ships in a later sprint
    # (Google Sheets / Code / OpenAI). Forwards input unchanged so flows
    # built around them stay runnable end-to-end.
    return [list(items) if items else [{}]]


EXECUTORS = {
    "manualTrigger": _exec_manual,
    "webhookTrigger": _exec_manual,     # triggers just pass their payload through
    "formTrigger": _exec_manual,
    "scheduleTrigger": _exec_manual,    # timer trigger seeds the flow like manual
    "httpRequest": _exec_http,
    "wait": _exec_wait,
    "if": _exec_if,
    "set": _exec_set,
    "respondToWebhook": _exec_respond,
    "googleSheets": _exec_passthrough,  # visual stub (real I/O = later sprint)
    "code": _exec_passthrough,          # visual stub (sandboxed run = later sprint)
    "openAi": _exec_passthrough,        # visual stub (model call = later sprint)
}


# ---------------------------------------------------------------------------
# The execution engine (§6)
# ---------------------------------------------------------------------------
def _edges(conns):
    """Flatten connections into (src, output_index, dst, input_index) tuples."""
    edges = []
    for src, outputs in (conns or {}).items():
        for oi, targets in enumerate((outputs or {}).get("main", []) or []):
            for tgt in (targets or []):
                if tgt and tgt.get("node"):
                    edges.append((src, oi, tgt["node"], tgt.get("index", 0)))
    return edges


def _reachable(start_id, edges):
    adj = {}
    for src, _oi, dst, _ii in edges:
        adj.setdefault(src, []).append(dst)
    seen, stack = {start_id}, [start_id]
    while stack:
        for nxt in adj.get(stack.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _topo(node_ids, edges):
    """Kahn topological order over the reachable subgraph (DAG; no loop-back in v1)."""
    incoming = {nid: 0 for nid in node_ids}
    adj = {nid: [] for nid in node_ids}
    for src, _oi, dst, _ii in edges:
        if src in node_ids and dst in node_ids:
            adj[src].append(dst)
            incoming[dst] += 1
    queue = [nid for nid in node_ids if incoming[nid] == 0]
    order = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for dst in adj[nid]:
            incoming[dst] -= 1
            if incoming[dst] == 0:
                queue.append(dst)
    order += [nid for nid in node_ids if nid not in order]   # leftover (cycle) -> best effort
    return order


def _find_trigger(nodes, trigger_node_id=None):
    if trigger_node_id:
        return next((n for n in nodes if n.get("id") == trigger_node_id), None)
    return next((n for n in nodes if n.get("type") in TRIGGER_TYPES), None)


def run_workflow(wf, trigger_payload=None, trigger_node_id=None, trigger_kind="manual"):
    """Run a workflow and return a run-log dict (status + per-node results)."""
    nodes = wf.get("nodes", []) or []
    by_id = {n["id"]: n for n in nodes if n.get("id")}
    edges = _edges(wf.get("connections", {}))
    trig = _find_trigger(nodes, trigger_node_id)

    run = {
        "workflow_id": wf.get("id"),
        "started_at": _now_iso(),
        "trigger": trigger_kind,
        "status": "success",
        "node_runs": [],
        "response": None,          # filled by a respondToWebhook node, if any
        "error": None,
    }
    if not trig:
        run["status"] = "failed"
        run["error"] = "no trigger node"
        run["finished_at"] = _now_iso()
        return run

    reachable = _reachable(trig["id"], edges)
    order = _topo(reachable, edges)
    node_inputs = {nid: [] for nid in reachable}
    node_inputs[trig["id"]] = [trigger_payload] if trigger_payload is not None else [{}]

    out_map = {}                                     # src -> {output_index: [dst, ...]}
    for src, oi, dst, _ii in edges:
        out_map.setdefault(src, {}).setdefault(oi, []).append(dst)

    for nid in order:
        node = by_id.get(nid)
        if not node:
            continue
        items_in = node_inputs.get(nid, [])
        nr = {"node_id": nid, "name": node.get("name", nid), "type": node.get("type"),
              "items_in": len(items_in), "items_out": 0, "output": [], "sample": [],
              "status": "success", "error": None}

        # A node only fires if it received items (the trigger is always seeded). §6
        if nid != trig["id"] and not items_in:
            nr["status"] = "skipped"
            run["node_runs"].append(nr)
            continue

        executor = EXECUTORS.get(node.get("type"))
        if executor is None:
            nr["status"] = "failed"
            nr["error"] = f"no executor for type {node.get('type')!r}"
            run["node_runs"].append(nr)
            run["status"] = "failed"
            run["error"] = nr["error"]
            break

        try:
            outputs = executor(node.get("parameters", {}) or {}, items_in) or [[]]
        except Exception as e:                       # hard error -> stop the run (§6)
            nr["status"] = "failed"
            nr["error"] = f"{type(e).__name__}: {e}"
            run["node_runs"].append(nr)
            run["status"] = "failed"
            run["error"] = nr["error"]
            break

        first = outputs[0] if outputs else []
        nr["items_out"] = sum(len(o or []) for o in outputs)
        nr["output"] = first
        nr["sample"] = first[:3]
        run["node_runs"].append(nr)

        if node.get("type") == "respondToWebhook" and run.get("response") is None:
            p = node.get("parameters", {}) or {}
            item0 = items_in[0] if items_in else {}
            run["response"] = {"statusCode": int(p.get("statusCode", 200) or 200),
                               "bodyType": p.get("bodyType", "json"),
                               "body": _render(p.get("body", ""), item0)}

        targets = out_map.get(nid, {})
        for oi, out_items in enumerate(outputs):
            for dst in targets.get(oi, []):
                if dst in node_inputs:
                    node_inputs[dst].extend(out_items or [])

    run["finished_at"] = _now_iso()
    return run


# ---------------------------------------------------------------------------
# Run logs + per-workflow stats (§6 "executed N times")
# ---------------------------------------------------------------------------
def _save_run(wf_id, run):
    d = RUNS_DIR / wf_id
    d.mkdir(parents=True, exist_ok=True)
    run["id"] = run.get("id") or uuid.uuid4().hex[:12]
    _atomic_write(d / f"{run['id']}.json", json.dumps(run, indent=2))
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_RUNS:]:
        try:
            p.unlink()
        except OSError:
            pass
    _update_stats(wf_id, run)
    return run["id"]


def _update_stats(wf_id, run):
    sd = STATE_DIR / wf_id
    sd.mkdir(parents=True, exist_ok=True)
    stats = _load_stats(wf_id)
    stats["total"] += 1
    if run.get("status") == "failed":
        stats["failed"] += 1
    else:
        stats["succeeded"] += 1
    stats["last_run_at"] = run.get("finished_at") or _now_iso()
    stats["last_status"] = run.get("status")
    _atomic_write(sd / "stats.json", json.dumps(stats, indent=2))


def _load_stats(wf_id):
    sp = STATE_DIR / wf_id / "stats.json"
    base = {"total": 0, "succeeded": 0, "failed": 0, "last_run_at": None, "last_status": None}
    if sp.exists():
        try:
            base.update(json.loads(sp.read_text()))
        except Exception:
            pass
    return base


def _list_runs(wf_id, limit=KEEP_RUNS):
    d = RUNS_DIR / wf_id
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Version history (§5.1) — snapshot on every save; restore is non-destructive.
# ---------------------------------------------------------------------------
def _snapshot_version(wf):
    if not wf:
        return
    d = VERSIONS_DIR / wf["id"]
    d.mkdir(parents=True, exist_ok=True)
    v = int(wf.get("version", 1))
    _atomic_write(d / f"{v}.json", json.dumps(wf, indent=2))
    snaps = sorted(d.glob("*.json"),
                   key=lambda p: int(p.stem) if p.stem.isdigit() else 0, reverse=True)
    for p in snaps[KEEP_VERSIONS:]:
        try:
            p.unlink()
        except OSError:
            pass


def _list_versions(wf_id):
    d = VERSIONS_DIR / wf_id
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"),
                    key=lambda p: int(p.stem) if p.stem.isdigit() else 0, reverse=True):
        try:
            wf = json.loads(p.read_text())
            out.append({"version": wf.get("version"), "saved_at": wf.get("updated_at"),
                        "name": wf.get("name"), "nodes": len(wf.get("nodes", []))})
        except Exception:
            pass
    return out


def _load_version(wf_id, v):
    p = VERSIONS_DIR / wf_id / f"{int(v)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP API — registered onto a Flask app. Pass the host app's auth decorators to
# reuse them; omit them (standalone dev) and the routes are open. (§14)
# ---------------------------------------------------------------------------
def init_automations(app, login_required=None, perm_required=None, base_path="/api/automations"):
    from flask import request, jsonify, render_template, Response

    lr = login_required or (lambda f: f)

    def guard(f):
        if perm_required is not None:
            f = perm_required("admin")(f)
        return lr(f)

    @app.route("/automations")
    @guard
    def automations_page():
        return render_template("automations.html")

    @app.route(f"{base_path}/_node-catalog")
    @guard
    def automations_catalog():
        return jsonify({"nodes": NODE_CATALOG})

    @app.route(base_path)
    @guard
    def automations_list():
        items = []
        for wid in _wf_list_ids():
            wf = _wf_load(wid)
            if not wf:
                continue
            items.append({"id": wf["id"], "name": wf.get("name"),
                          "active": wf.get("active", False), "version": wf.get("version", 1),
                          "updated_at": wf.get("updated_at"), "stats": _load_stats(wid)})
        return jsonify({"workflows": items})

    @app.route(base_path, methods=["POST"])
    @guard
    def automations_create():
        b = request.get_json(silent=True) or {}
        return jsonify(create_workflow(b.get("name") or "Untitled workflow")), 201

    @app.route(f"{base_path}/<wid>")
    @guard
    def automations_get(wid):
        wf = _wf_load(wid)
        return (jsonify(wf), 200) if wf else (jsonify({"error": "not found"}), 404)

    @app.route(f"{base_path}/<wid>", methods=["PUT"])
    @guard
    def automations_put(wid):
        existing = _wf_load(wid)
        if not existing:
            return jsonify({"error": "not found"}), 404
        b = request.get_json(silent=True) or {}
        wf = dict(existing)
        for k in ("name", "description", "active", "nodes", "connections"):
            if k in b:
                wf[k] = b[k]
        wf["id"] = wid
        err = _wf_validate(wf)
        if err:
            return jsonify({"error": err}), 400
        _snapshot_version(existing)                      # keep the pre-save version (§5.1)
        wf["version"] = int(existing.get("version", 1)) + 1
        wf["updated_at"] = _now_iso()
        _ensure_tokens(wf)                                # assign webhook/form URLs (§7.6/7.7)
        _wf_save(wf)
        return jsonify(wf)

    @app.route(f"{base_path}/<wid>", methods=["DELETE"])
    @guard
    def automations_delete(wid):
        p = _wf_path(wid)
        if p.exists():
            p.unlink()
        return jsonify({"ok": True})

    @app.route(f"{base_path}/<wid>/run", methods=["POST"])
    @guard
    def automations_run(wid):
        wf = _wf_load(wid)
        if not wf:
            return jsonify({"error": "not found"}), 404
        b = request.get_json(silent=True) or {}
        run = run_workflow(wf, trigger_payload=b.get("payload"), trigger_kind="manual")
        _save_run(wid, run)
        return jsonify(run)

    @app.route(f"{base_path}/<wid>/runs")
    @guard
    def automations_runs(wid):
        return jsonify({"runs": _list_runs(wid)})

    # ---- version history (§5.1) ----
    @app.route(f"{base_path}/<wid>/versions")
    @guard
    def automations_versions(wid):
        return jsonify({"versions": _list_versions(wid)})

    @app.route(f"{base_path}/<wid>/versions/<int:v>")
    @guard
    def automations_version_get(wid, v):
        snap = _load_version(wid, v)
        return (jsonify(snap), 200) if snap else (jsonify({"error": "not found"}), 404)

    @app.route(f"{base_path}/<wid>/restore/<int:v>", methods=["POST"])
    @guard
    def automations_restore(wid, v):
        cur = _wf_load(wid)
        snap = _load_version(wid, v)
        if not cur or not snap:
            return jsonify({"error": "not found"}), 404
        _snapshot_version(cur)                            # snapshot current first -> restore is undoable
        restored = dict(snap)
        restored["id"] = wid
        restored["version"] = int(cur.get("version", 1)) + 1
        restored["updated_at"] = _now_iso()
        _wf_save(restored)
        return jsonify(restored)

    # ---- export / import (§5.2) ----
    @app.route(f"{base_path}/<wid>/export")
    @guard
    def automations_export(wid):
        wf = _wf_load(wid)
        if not wf:
            return jsonify({"error": "not found"}), 404
        clean = {k: v for k, v in wf.items() if k not in ("updated_at",)}
        fname = (wf.get("id") or "workflow") + ".json"
        return Response(json.dumps(clean, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.route(f"{base_path}/import", methods=["POST"])
    @guard
    def automations_import():
        b = request.get_json(silent=True)
        if not isinstance(b, dict):
            return jsonify({"error": "invalid JSON"}), 400
        now = _now_iso()
        wid = _slugify(b.get("name") or "Imported workflow", set(_wf_list_ids()))
        wf = {"id": wid, "name": (b.get("name") or "Imported workflow")[:80],
              "description": b.get("description", ""), "active": False, "version": 1,
              "nodes": b.get("nodes") or [], "connections": b.get("connections") or {},
              "created_at": now, "updated_at": now}
        err = _wf_validate(wf)
        if err:
            return jsonify({"error": err}), 400
        _ensure_tokens(wf)
        _wf_save(wf)
        return jsonify(wf), 201

    # ---- active on/off (§7.9 / Sprint 3) ----
    @app.route(f"{base_path}/<wid>/activate", methods=["POST"])
    @guard
    def automations_activate(wid):
        wf = _wf_load(wid)
        if not wf:
            return jsonify({"error": "not found"}), 404
        b = request.get_json(silent=True) or {}
        wf["active"] = bool(b.get("active", not wf.get("active")))
        wf["updated_at"] = _now_iso()
        _wf_save(wf)
        return jsonify(wf)

    # ---- PUBLIC trigger routes — no login gate; protected by the unguessable token ----
    @app.route(f"{base_path}/hook/<token>",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def automations_hook(token):
        if request.method == "OPTIONS":
            r = Response("", status=204)
        else:
            wf, node = _find_by_token(token, "webhookTrigger")
            if not wf or not node:
                return jsonify({"error": "unknown webhook"}), 404
            if not wf.get("active"):
                return jsonify({"error": "workflow is inactive"}), 403
            p = node.get("parameters", {}) or {}
            if p.get("auth") == "header secret" and request.headers.get("X-Webhook-Secret", "") != (p.get("secret") or ""):
                return jsonify({"error": "bad or missing X-Webhook-Secret"}), 401
            body = request.get_json(silent=True)
            if body is None:
                body = request.form.to_dict() or {}
            item = {"body": body, "query": request.args.to_dict(),
                    "headers": {k: v for k, v in request.headers.items()}}
            run = run_workflow(wf, trigger_payload=item, trigger_node_id=node["id"], trigger_kind="webhook")
            _save_run(wf["id"], run)
            resp = run.get("response")
            if p.get("responseMode") == "usingRespondNode" and resp:
                if resp.get("bodyType") == "json":
                    raw = resp.get("body") or ""
                    try:
                        data = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
                    except Exception:
                        data = {"raw": raw}
                    r = Response(json.dumps(data), status=resp.get("statusCode", 200), mimetype="application/json")
                else:
                    r = Response(resp.get("body", ""), status=resp.get("statusCode", 200), mimetype="text/plain")
            else:
                r = jsonify({"ok": True, "status": run["status"]})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Webhook-Secret"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return r

    @app.route("/automations/form/<token>", methods=["GET"])
    def automations_form_page(token):
        wf, node = _find_by_token(token, "formTrigger")
        if not wf or not node or not wf.get("active"):
            return ("This form is not available.", 404)
        return render_template("form.html", cfg=node.get("parameters", {}) or {}, token=token, wf_name=wf.get("name"))

    @app.route("/automations/form/<token>", methods=["POST"])
    def automations_form_submit(token):
        wf, node = _find_by_token(token, "formTrigger")
        if not wf or not node or not wf.get("active"):
            return ("This form is not available.", 404)
        p = node.get("parameters", {}) or {}
        item = {}
        for f in (p.get("fields") or []):
            lbl = f.get("label")
            if lbl:
                item[lbl] = request.form.get(lbl, "")
        run = run_workflow(wf, trigger_payload=item, trigger_node_id=node["id"], trigger_kind="form")
        _save_run(wf["id"], run)
        done = p.get("submitMessage") or "Thanks! Your submission was received."
        return render_template("form.html", cfg=p, token=token, wf_name=wf.get("name"), done=done)

    app.logger.info("[automations] ready — %d node types, storage=%s", len(NODE_CATALOG), AUTOMATIONS_DIR)
    return app
