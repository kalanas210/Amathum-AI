# Sprint & Build Guide — Naxter Automation Workflow Builder ("n8n inside our app")

**Feature codename:** `automations`
**Audience:** the developer who will build this (junior‑friendly — every step is explained)
**Status:** Sprint 1 (backend foundation) already coded; Sprints 2–5 to do
**Goal in one line:** Let our internal team build "if‑this‑then‑that" automations with a drag‑and‑drop canvas — webhook / form / Google‑Sheets triggers that run HTTP calls, waits, branching and Sheets writes — **native inside our existing Flask admin panel**.

> Read this top to bottom once before writing any code. It tells you **what** to build, **how** to build it, **every node in detail**, **all the environment variables**, and a **click‑by‑click guide to get the Google Client ID & Secret**.

---

## Table of contents

1. [The 30‑second summary](#1-the-30second-summary)
2. [Background — what already exists](#2-background--what-already-exists)
3. [Glossary (read this — 9 words)](#3-glossary-read-this--9-words)
4. [Architecture overview](#4-architecture-overview)
5. [The data model (the workflow JSON)](#5-the-data-model-the-workflow-json)
6. [The execution engine — how a workflow runs](#6-the-execution-engine--how-a-workflow-runs)
7. [Node‑by‑node build guide](#7-nodebynode-build-guide) ← the heart of the doc
8. [The visual canvas (front‑end)](#8-the-visual-canvas-frontend)
9. [Triggers deep‑dive](#9-triggers-deepdive)
10. [Google integration — full walkthrough + Client ID/Secret](#10-google-integration--full-walkthrough)
11. [Environment variables & secrets (complete list)](#11-environment-variables--secrets-complete-list)
12. [Security checklist](#12-security-checklist)
13. [Storage & file layout](#13-storage--file-layout)
14. [API reference](#14-api-reference)
15. [Sprint breakdown (tickets, estimates, acceptance criteria)](#15-sprint-breakdown)
16. [How to deploy](#16-how-to-deploy)
17. [Definition of Done](#17-definition-of-done)
18. [Risks & out‑of‑scope](#18-risks--outofscope)
19. [Appendix A — example workflows](#appendix-a--example-workflows)
20. [Appendix B — references](#appendix-b--references)

---

## 1. The 30‑second summary

We are building a small clone of **n8n** (the popular automation tool) **inside our own app**, because:

- It must live **inside our Flask admin panel** with our own login, our own database files, and our own look.
- It is for our **internal team only** (this matters for the Google setup — see §10).

A user will open a page, **drag boxes (nodes) onto a canvas**, **draw lines between them**, configure each box, and press **Run**. Behind the scenes a **Python engine** runs the boxes in order, passing data from one to the next.

There are two kinds of boxes:

- **Triggers** — they *start* a workflow (a webhook is called, a form is submitted, a Google Sheet changes, or you press *Run*).
- **Actions / logic** — they *do* something (call an API, wait, branch with IF, edit data, write to a Google Sheet).

---

## 2. Background — what already exists

You are **not starting from zero**. Sprint 1 (the hardest part — the engine) is already coded and tested.

| Thing | Where | State |
|---|---|---|
| Backend module | `pbx-monitor/automations.py` | ✅ built & unit‑tested |
| Wired into the app | `pbx-monitor/app.py` (2 lines before `__main__`) | ✅ done |
| Source‑of‑truth copy | `staging/automations.py` + `staging/app.py` | ✅ in sync |
| Visual canvas page | `templates/automations.html` + `static/automations.js` | ❌ Sprint 2 |
| Real triggers (webhook/form/sheets) | `automations.py` | ❌ Sprints 3–4 |
| Google OAuth + Sheets | `automations.py` + `instance/google.json` | ❌ Sprint 4 |

**Our app stack (important — work *with* it, not against it):**

- **Backend:** Python **Flask**. One big file `app.py` (~3,300 lines). Pages are HTML templates (Jinja2). **No database** — data is stored as **JSON files on disk**.
- **Front‑end:** plain HTML + **vanilla JavaScript** + **Tailwind CSS from a CDN**. **There is NO build step** (no webpack/npm build). The existing "Flows" page already loads **React Flow** (the node‑canvas library) straight from a CDN (`esm.sh`) — we copy that exact trick.
- **Auth:** every admin route uses two decorators: `@login_required` and `@perm_required('admin')`.
- **Deploy:** edit files in `staging/`, then run an install script that copies them to `/opt/pbx-monitor` on the server and restarts the `pbx-monitor` systemd service.

> **Why a separate `automations.py` module?** So this big feature stays out of the giant `app.py`. It plugs in with a single function call and **cannot crash the phone panel** (the call is wrapped in `try/except`).

**Relationship to the existing "Flows" page:** the Flows page *looks* similar (boxes and wires) but does a different job — it turns a diagram into *instructions for the AI voice agent*. **Our automations builder is separate** and turns a diagram into a *running program*. Don't mix them.

---

## 3. Glossary (read this — 9 words)

| Word | Plain meaning |
|---|---|
| **Workflow / Automation** | One saved diagram of boxes + lines. Stored as one JSON file. |
| **Node** | A box on the canvas. Has a *type* (e.g. "HTTP Request") and *parameters*. |
| **Edge / Connection** | A line from one node's output to another node's input. |
| **Trigger** | A node that *starts* the workflow. Always the first node. |
| **Action** | A node that *does* work (HTTP call, write a sheet, etc.). |
| **Item** | One piece of data flowing between nodes — a JSON object `{ ... }`. Nodes pass a **list of items**. |
| **Expression** | A placeholder like `{{ $json.email }}` that gets replaced with real data while running. |
| **Run / Execution** | One time the workflow actually ran. We save a log of each run. |
| **Credential** | A saved secret (e.g. the Google login token) a node uses. |

---

## 4. Architecture overview

```
                        BROWSER (admin user)
        ┌───────────────────────────────────────────────┐
        │  /automations  (one HTML page)                 │
        │  ┌───────────┐   ┌──────────────┐  ┌────────┐  │
        │  │  Palette  │   │   Canvas     │  │ Config │  │
        │  │ (node     │ → │  (React Flow │  │ panel  │  │
        │  │  list)    │   │   boxes+wires│  │        │  │
        │  └───────────┘   └──────────────┘  └────────┘  │
        │           [ Save ]  [ ▶ Run ]   run‑log        │
        └───────────────┬───────────────────────────────┘
                        │  fetch() JSON API
                        ▼
        ┌───────────────────────────────────────────────┐
        │  FLASK  (app.py  →  automations.py)            │
        │                                                │
        │  Routes:  /api/automations/...                 │
        │  Engine:  run_workflow()  ← runs the nodes     │
        │  Triggers: webhook URL, form page, scheduler   │
        │  Google:  OAuth connect + token store          │
        └───────────────┬───────────────────────────────┘
                        │  reads / writes JSON files
                        ▼
        /var/lib/sampath-ai/automations/<id>.json        (the workflows)
        /var/lib/sampath-ai/automations-runs/<id>/...     (run logs)
        pbx-monitor/instance/google.json                  (OAuth secrets)
        pbx-monitor/instance/automations-credentials.json (encrypted tokens)
```

**The big idea:** the **front‑end only draws and edits** the diagram. The **back‑end runs it**. The diagram is saved as JSON; the engine reads that JSON and executes it.

---

## 5. The data model (the workflow JSON)

Every workflow is **one JSON file**. This is the contract between the canvas and the engine — get it right and everything else is easy. (Already implemented in `automations.py`.)

```json
{
  "id": "lead-to-sheet",
  "name": "Save new leads to a sheet",
  "description": "When the website form is submitted, add a row to Google Sheets.",
  "active": false,

  "nodes": [
    {
      "id": "trigger",
      "name": "Website form",
      "type": "formTrigger",
      "position": { "x": 80, "y": 160 },
      "parameters": { "fields": [ {"label": "Email", "type": "email", "required": true} ] }
    },
    {
      "id": "http",
      "name": "Notify Slack",
      "type": "httpRequest",
      "position": { "x": 380, "y": 160 },
      "parameters": { "method": "POST", "url": "https://hooks.slack.com/...", "bodyType": "json", "body": "{\"text\":\"New lead: {{ $json.Email }}\"}" }
    }
  ],

  "connections": {
    "trigger": { "main": [ [ { "node": "http", "index": 0 } ] ] }
  },

  "created_at": "2026-06-01T10:00:00Z",
  "updated_at": "2026-06-01T10:05:00Z"
}
```

### Field rules

| Field | Rule |
|---|---|
| `id` | lowercase letters/numbers/hyphens, 2–63 chars. Regex: `^[a-z0-9][a-z0-9-]{1,62}$`. Generated from the name on create; never changes. |
| `name` | 1–80 chars. Shown to the user. |
| `nodes[]` | Each node needs a **unique `id`**, a `type` from the catalog, a `position` (canvas x/y), and `parameters` (an object whose shape depends on the type — see §7). |
| `connections` | An object **keyed by the SOURCE node's `id`**. Inside, `main` is a list **indexed by the source node's output number**. Each output holds a list of targets `{ "node": "<target id>", "index": 0 }`. |

### How to read `connections` (important)

`connections["A"]["main"][0]` = "the list of nodes connected to **A's first output**".

- A normal node has **one output** → `main[0]`.
- An **IF** node has **two outputs** → `main[0]` is the **TRUE** branch, `main[1]` is the **FALSE** branch.

Example: IF node `cond` sends TRUE to `sendEmail` and FALSE to `logIt`:

```json
"cond": { "main": [
  [ { "node": "sendEmail", "index": 0 } ],   // output 0 = TRUE
  [ { "node": "logIt",     "index": 0 } ]    // output 1 = FALSE
] }
```

> **Why key by `id` and not by name (n8n keys by name)?** Because users rename nodes. Keying by the permanent `id` means renames never break connections.

### 5.1 Version history (see & restore previous versions)

Users must be able to view older versions of a workflow and **roll back**. We have **no SQL database**, so — like everything else here — versions are kept as **JSON files** on disk.

**How it works:**

- **On every Save** (`PUT /api/automations/<id>`), *before* overwriting the live file, copy the current file to a snapshot at `automations-versions/<id>/<version>.json`.
- Each workflow gains a **`version`** integer that **increments on every save** (shown in the UI as "v12"). Each snapshot also records `saved_by` + `saved_at`.
- Keep the **last ~30** versions per workflow; prune older ones (same pattern as run‑logs).
- **Restore is non‑destructive:** restoring `v7` first snapshots the *current* version, then writes `v7` back as the new live version — so a restore can itself be undone.

**UI (Sprint 2):** a **History** panel opened from a `history` (clock) icon in the builder header, listing each version — *v12 · saved by Alice · 2 days ago* — with **View** (open read‑only) and **Restore** on each row. Optionally show a changed‑node count between versions.

**API:** see §14 — `/versions`, `/versions/<v>`, `/restore/<v>`.

**Gotcha:** snapshot **only on an explicit Save**, never on every keystroke. Snapshots are full copies (workflows are tiny, so this is fine).

### 5.2 Export & import (JSON — like n8n)

Just like n8n, a user can **download a workflow as a JSON file** and **upload a JSON file to recreate it** — for backups, sharing, and moving a workflow between machines.

**Export:**

- A `download` icon on the builder header **and** on each row of the workflow list.
- `GET /api/automations/<id>/export` returns the workflow JSON with header `Content-Disposition: attachment; filename="<name>.json"` so the browser downloads it.
- The file is **our workflow JSON** (the §5 schema). Optionally strip volatile fields (`updated_at`, `version`, `updated_by`) so exports are clean and diff‑friendly.

**Import:**

- An **Import** button on the list → file picker → choose a `.json`.
- `POST /api/automations/import` → **validate** with `_wf_validate` (reject unknown node types, malformed shape, files > 256 KB), then **create a NEW workflow** (fresh `id`, `version = 1`, **inactive**). An import **never overwrites** an existing automation.
- Open the new workflow in the builder.

**"Just like n8n" — read this:** this gives the **same capability** as n8n (download/upload JSON). The **file shape is ours**, not n8n's (we key connections by node *id*; n8n keys by *name*). Importing a file exported from the *real* n8n needs a **separate converter** (map n8n's nodes/connections to ours) — list that as an **optional enhancement**, not v1.

**Security:** never trust an uploaded file — validate it, cap its size, strip any secrets, and never auto‑activate an imported workflow.

---

## 6. The execution engine — how a workflow runs

This is already built (`run_workflow()` in `automations.py`). You must **understand it** to add nodes correctly.

### The "items" rule

Data between nodes is **always a list of items**, where each item is a JSON object. Example: `[ {"email": "a@x.com"}, {"email": "b@x.com"} ]` is two items. A node usually runs its logic **once per item**.

### The run loop (simplified)

1. **Find the trigger node** (the one whose type is a trigger). Its output is the starting data (e.g. the form submission, or `[{}]` for a manual run).
2. **Walk the graph** following the connections (breadth‑first). For each node:
   - take the items that arrived on its input,
   - run the node's **executor** function → it returns a **list of outputs** (one list of items per output number),
   - send each output's items along the matching connection to the next node(s).
3. A node **only fires if it actually received items** (so an IF's unused branch does nothing — same as n8n).
4. Record a **per‑node log** (status, how many items in/out, a small sample, any error). Save the whole run.

### Each executor's shape (memorise this)

```python
def _exec_<nodetype>(params, items):
    # params = the node's "parameters" object
    # items  = list of input items (dicts)
    # RETURN: a list of output-lists, one per output.
    #   single-output node  -> return [ out_items ]
    #   IF node (2 outputs)  -> return [ true_items, false_items ]
    return [ out_items ]
```

Then register it: `EXECUTORS["<nodetype>"] = _exec_<nodetype>`.

### Expressions (`{{ ... }}`)

Any text parameter can contain `{{ $json.fieldName }}`. While running, that gets replaced with the value of `fieldName` from the **current item**. Supports dotted paths: `{{ $json.user.address.city }}`. (Helper `_render(value, item)` already does this.) **No `eval`, no code execution** — it's a safe find‑and‑replace.

### Run status & stats (the "executed N times" number)

Every run produces a **run‑level status**, not just per‑node logs:

- `success` — every node that fired finished OK.
- `failed` — a node hit a hard error and the run stopped.
- `partial` — the run finished but a node reported a *soft* error (see below). In v1 you may treat these as `success` with a warning.

After **every** run (manual, webhook, form, or scheduler), update a small per‑workflow **stats** file at `automations-state/<id>/stats.json`:

```json
{ "total": 42, "succeeded": 39, "failed": 3, "last_run_at": "2026-06-01T10:30:00Z", "last_status": "success" }
```

`GET /api/automations` returns these so the **workflow list** and the **builder header** can show:
**"Executed 42 times · 39 ok / 3 failed · last run 2 min ago"**. (Ticket **AUTO‑208**.)

### Error handling — what happens when a node fails

This is what "manage errors correctly" means — be deliberate:

- **Hard error** (a node's executor *raises* an exception): that path **stops**, the run is marked **`failed`**, and the run log records the **failing node's id + the error message**. Downstream nodes do not run. *(The engine already does this — see `run_workflow`.)*
- **Soft error** (a node returns an error *inside its data*, e.g. the HTTP node's `{ "error": ... }` on a network failure): the run **continues**, so you can branch on it with an IF. This is the difference between "the step broke" and "the step worked and the answer was an error."
- **Retries** (Sprint 5, AUTO‑503): a node can opt in to `retryOnFail` with `maxRetries` (default 2) + a short backoff. Only **hard** errors retry; after the last attempt it becomes a hard error.
- **In the UI:** the failing node is shown **red on the canvas** *and* as a ❌ row in the run‑log; clicking that row **jumps to the node**. A failed run shows a red status badge — errors are never hidden.
- **Non‑manual runs** (webhook/form/scheduler) write the **same** run log, viewable in the **run‑history** screen (AUTO‑504) — so their errors are not lost just because nobody was watching the screen.

### Engine upgrades for Sprint 6 (cross‑node data, `$now`, secrets) 🔨

> The 5 Sprint‑1 executors live in `automations.py` already — read them as your template. The Sprint‑6 features need the engine to expose a bit more, so here is the upgrade (fully backwards‑compatible — every old `{{ $json.x }}` still works).

The Sprint‑1 resolver only sees the **current item**. Stronger expressions, the Code node, and secrets need it to also see **other nodes' outputs**, **the time**, and **named secrets**. Extend `_lookup` / `_render` and `run_workflow`:

```python
# automations.py — Sprint 6 engine upgrade
import datetime
from automations_secrets import get_secret          # the named-secret store (see §11.5)

_NODEREF_RE = re.compile(r'^\$node\[[\'"](.+?)[\'"]\]\.?(.*)$')

def _walk(obj, dotted):
    """Follow a dotted path (a.b.0.c) through dicts/lists; None if missing."""
    cur = obj
    for part in filter(None, dotted.split('.')):
        if isinstance(cur, dict):       cur = cur.get(part)
        elif isinstance(cur, list):
            try: cur = cur[int(part)]
            except (ValueError, IndexError): return None
        else: return None
    return cur

def _lookup(path, item, ctx=None):
    """Resolve one {{ expression }}. ctx carries other nodes' output + helpers."""
    path = path.strip(); ctx = ctx or {}
    m = _NODEREF_RE.match(path)                       # {{ $node["HTTP Request"].body.id }}
    if m:
        return _walk((ctx.get('nodes') or {}).get(m.group(1)), m.group(2))
    if path.startswith('$secrets.'):                  # {{ $secrets.SLACK_TOKEN }}  (server-side only)
        return get_secret(path[len('$secrets.'):])
    if path == '$now':                                # {{ $now }} -> ISO timestamp
        return ctx.get('now')
    if path.startswith('$json.'): path = path[len('$json.'):]
    elif path == '$json':         return item
    return _walk(item, path)

def _render(value, item, ctx=None):
    if not isinstance(value, str) or '{{' not in value:
        return value
    def repl(m):
        r = _lookup(m.group(1), item, ctx)
        if r is None: return ''
        return json.dumps(r) if isinstance(r, (dict, list)) else str(r)
    return _EXPR_RE.sub(repl, value)
```

In `run_workflow`, remember each node's output and pass a small **context** to executors:

```python
    node_outputs = {}                       # node NAME -> its first output item (for $node refs)
    def _ctx():
        return {'nodes': node_outputs, 'now': _now_iso()}

    # ... where Sprint-1 calls `outputs = executor(params, items_in)`, now pass ctx:
    #     outputs = executor(params, items_in, _ctx())
    # ... and after it runs, record the output for later $node references:
    first = outputs[0] if outputs else []
    node_outputs[node.get('name', nid)] = first[0] if first else {}
```

> **One small refactor:** change the executor signature from `(params, items)` to `(params, items, ctx=None)`. The five built executors just add `ctx=None`; `_exec_set` / `_exec_if` / `_exec_http` pass `ctx` into their `_render(...)` calls. Nothing else changes.

---

## 7. Node‑by‑node build guide

This is the most important section. **Every node** has the same structure so you can build them one by one:

> **What it does** · **Type id** · **Inputs/Outputs** · **Config fields** · **Build it (backend)** · **Build it (front‑end config panel)** · **Example** · **Gotchas**

Legend: ✅ = already coded in Sprint 1 · 🔨 = you build it.

---

### 7.1 Manual / Test trigger ✅

- **What it does:** starts the workflow when the user clicks **Run**. For building & testing only — not for production.
- **Type id:** `manualTrigger`
- **Inputs/Outputs:** no input · 1 output.
- **Config fields:** none.
- **Backend:** returns `[[{}]]` (one output, one empty item) — or the test payload the user typed in the Run dialog.
- **Front‑end panel:** just a sentence: "This workflow starts when you press Run."
- **Gotchas:** every workflow needs exactly **one** trigger; for a brand‑new workflow this is the default first node.

---

### 7.2 HTTP Request ✅

- **What it does:** calls any web API. This is the most‑used action.
- **Type id:** `httpRequest`
- **Inputs/Outputs:** 1 input · 1 output. Runs **once per item**.
- **Config fields:**

| Field | Control | Default | Notes |
|---|---|---|---|
| `method` | dropdown | `GET` | GET / POST / PUT / PATCH / DELETE |
| `url` | text | — | Supports expressions, e.g. `https://api.x.com/users/{{ $json.id }}` |
| `headers` | key‑value editor (JSON) | `{}` | e.g. `{ "Authorization": "Bearer {{ $json.token }}" }` |
| `bodyType` | dropdown | `none` | `none` / `json` / `raw` |
| `body` | textarea | "" | Used when bodyType is `json` or `raw`. Supports expressions. |

- **Backend:** for each item — render `url`, `headers`, `body` (expressions), build the request, call it with the chosen method using Python's **stdlib `urllib`** (no extra library to install), **20‑second timeout**, read at most **1 MB** of response. Attach the result to the item under `_http`: `{ statusCode, headers, body }` (body is parsed to JSON automatically if the response is JSON). Network errors don't crash the run — they return `{ "error": "..." }`. *(Already implemented.)*
- **Front‑end panel:** method dropdown, URL text box, a small "add header" key/value list, body‑type dropdown, body textarea (hidden when body type is `none`).
- **Example:** GET `https://api.github.com/repos/{{ $json.repo }}` → next node can read `{{ $json._http.body.stargazers_count }}`.
- **Gotchas:**
  - **SSRF risk** (see §12): a user could point the URL at an internal address. For an internal‑only tool this is acceptable, but add an **allow/deny note** and a timeout.
  - If the API needs auth, put the token in a **header** (later, a "Credentials" feature can store these; for now headers are fine).

---

### 7.3 Wait ✅ (Sprint 1 = simple · Sprint 5 = durable)

- **What it does:** pauses before the next step.
- **Type id:** `wait`
- **Inputs/Outputs:** 1 in · 1 out (passes items through unchanged).
- **Config fields:** `amount` (number), `unit` (seconds / minutes / hours).
- **Backend (Sprint 1 — done):** sleeps inline, **capped at 15 seconds** (so a long wait can't freeze the web request). A "2 hours" request currently sleeps 15s and continues.
- **Backend (Sprint 5 — to build):** a **durable** wait. Instead of sleeping, **save the run's state to disk** with a "resume at <time>", return immediately, and let a **background scheduler** (APScheduler) pick it up and continue the run later. This survives restarts. This is the single hardest upgrade — budget for it.
- **Front‑end panel:** a number box + a unit dropdown. Add a note: "Long waits become reliable in a later release."
- **Gotchas:** never `sleep()` for minutes inside a web request in production — that ties up a worker. The 15s cap is the guard until durable wait exists.

---

### 7.4 IF (branch) ✅

- **What it does:** splits the flow into a **TRUE path** and a **FALSE path**.
- **Type id:** `if`
- **Inputs/Outputs:** 1 in · **2 outputs** (0 = true, 1 = false).
- **Config fields:** `value1`, `operator`, `value2`.
  - Operators: `equal`, `notEqual`, `contains`, `notContains`, `isEmpty`, `isNotEmpty`, `gt`, `lt`, `gte`, `lte`, `regex`.
- **Backend:** for each item, render `value1`/`value2` (expressions), compare with `operator`; matching items go to output 0, the rest to output 1. Returns `[true_items, false_items]`. *(Already implemented.)*
- **Front‑end panel:** value1 box, operator dropdown, value2 box. On the canvas, the IF node needs **two labelled output handles**: "true" (top) and "false" (bottom).
- **Example:** `value1 = {{ $json._http.statusCode }}`, operator `equal`, `value2 = 200` → success goes one way, errors the other.
- **Gotchas:** number comparisons (`gt`,`lt`,…) only work if both sides look like numbers, else they return false. Make this clear in the panel help text.

---

### 7.5 Edit Fields (Set) ✅

- **What it does:** add, rename, or set fields on the data passing through — the everyday "glue" node.
- **Type id:** `set`
- **Inputs/Outputs:** 1 in · 1 out.
- **Config fields:** `assignments` (a list of `{ name, value }`), `keepOnlySet` (checkbox).
- **Backend:** for each item, set each `name = render(value)`. If `keepOnlySet` is on, start from an empty object (drop everything else); otherwise keep existing fields and add/overwrite. *(Already implemented.)*
- **Front‑end panel:** a repeatable list of "field name" + "value" rows, plus a "keep only these fields" checkbox.
- **Example:** set `fullName = {{ $json.first }} {{ $json.last }}`.
- **Gotchas:** none major — this is the simplest action.

---

### 7.6 Webhook trigger 🔨 (Sprint 3)

- **What it does:** starts the workflow when an outside system sends an HTTP request to a **unique URL** we give it.
- **Type id:** `webhookTrigger`
- **Inputs/Outputs:** no input · 1 output (the output item is the incoming request: its JSON body, query params, and headers).
- **Config fields:**

| Field | Control | Notes |
|---|---|---|
| `method` | dropdown | which HTTP method the URL accepts (GET/POST/…) |
| `path` | text (read‑only token) | auto‑generated unique token; the full URL is shown to copy |
| `auth` | dropdown | `none` / `header secret` (a secret header the caller must send) |
| `responseMode` | dropdown | `immediately` (return 200 right away) / `usingRespondNode` (wait for a "Respond to Webhook" node) |

- **Build it (backend):**
  1. Add a **public route** (NOT admin‑gated): `@app.route("/api/automations/hook/<token>", methods=["GET","POST",...])`.
  2. On a request: find the workflow whose webhook node has that `token` (keep a small index file `webhooks.json` mapping `token → workflow_id` so lookup is fast).
  3. If `auth = header secret`, check the secret header; reject with 401 if wrong.
  4. Build the first item from the request: `{ "body": <json>, "query": <dict>, "headers": <dict> }`.
  5. Call `run_workflow(wf, trigger_payload=item)`.
  6. Respond per `responseMode`.
- **Build it (front‑end):** show the **full webhook URL** (built from `AUTOMATIONS_BASE_URL` + `/api/automations/hook/<token>`) with a copy button, a method dropdown, and the auth choice.
- **Use it on your OWN website (the main use case):** the user **copies the webhook URL** and makes their site send entries to it. Two ways:
  - **HTML form** (simplest) — point a form straight at the URL:
    ```html
    <form method="POST" action="https://monitor.easmoney.me/api/automations/hook/AbC123token">
      <input name="email"><input name="name"><button>Send</button>
    </form>
    ```
    Each field arrives inside `body`. ⚠️ A plain HTML `<form>` **cannot send a custom header**, so for form posts set the node's `auth = none` (rely on the long, unguessable token) — or use `fetch` below.
  - **JavaScript `fetch`** (when you can add a secret header) — paste on the site:
    ```js
    fetch("https://monitor.easmoney.me/api/automations/hook/AbC123token", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Webhook-Secret": "the-secret" },
      body: JSON.stringify({ email, name })
    });
    ```
  - **CORS note:** if the external site calls us from the browser, our hook route must return permissive CORS headers — or have the site call us from *its own* server.
- **Example:** your marketing site's contact form posts to the webhook URL → the workflow writes the lead to a Google Sheet and pings Slack.
- **Gotchas:**
  - The hook route must be **public** but protected by the **secret header** (and ideally rate‑limited).
  - Generate `token` with `secrets.token_urlsafe(24)` — long and unguessable.
  - Make the receiver **idempotent‑friendly** (callers sometimes retry).

---

### 7.7 Form trigger 🔨 (Sprint 3)

- **What it does:** gives you a ready‑made hosted web form; submitting it starts the workflow.
- **Type id:** `formTrigger`
- **Inputs/Outputs:** no input · 1 output (the submitted form values as one item).
- **Config fields:** `fields` — a list of `{ label, type, required }` where `type` ∈ `text / email / number / textarea / select(+options)`. Optional `title`, `description`, `submitMessage`.
- **Build it (backend):**
  1. A **public GET** route `/automations/form/<token>` renders an HTML form from the `fields` config (reuse the Tailwind styles in `base.html`).
  2. A **public POST** route receives the submission, turns it into one item `{ <label>: <value>, ... }`, and calls `run_workflow`.
  3. Show the `submitMessage` (e.g. "Thanks!") after submit.
- **Build it (front‑end, in the builder):** a small editor to add/remove fields (label, type, required) + a "copy form link" button.
- **Example:** an internal "request laptop" form → workflow opens a ticket via HTTP + writes a row to a sheet.
- **Gotchas:** validate `required` fields server‑side; add a simple anti‑spam (the existing app already has a CAPTCHA gate component you can copy if needed).

---

### 7.8 Google Sheets trigger 🔨 (Sprint 4)

- **What it does:** fires when a row is **added (create), updated, or deleted** in a Google Sheet.
- **Type id:** `sheetsTrigger`
- **Reality check (tell the user):** Google Sheets has **no instant "ping on edit"**. There are two ways, and **polling is the default** (it's what n8n itself does):

| Way | How | Speed | Effort |
|---|---|---|---|
| **Polling (default, build this)** | A background job checks the sheet every minute and compares to last time | up to ~60s delay | Low — no public URL, no per‑sheet setup |
| Apps Script webhook (optional, later) | A tiny script on the sheet POSTs to our webhook on edit | a few seconds | Medium — must add a script to each sheet |

- **Config fields:** `document` (spreadsheet ID or pick), `sheet` (tab name), `event` (`rowAdded` / `rowUpdated` / `rowDeleted` / `rowAddedUpdatedOrDeleted`), `keyColumn` (a column holding a **stable unique id** per row, e.g. `Email` or `ID` — **required to detect updates and deletes reliably**), `pollInterval` (default 1 min, **min 1 min**).
- **Build it (backend — polling):**
  1. A **background scheduler** (APScheduler) runs every minute.
  2. For each active workflow with a `sheetsTrigger`, read the sheet via the **Google Sheets API** (using the connected account's token — see §10).
  3. Compare to a saved snapshot **keyed by `keyColumn`**, then diff three ways:
     - **added (create)** = key present now, not in the snapshot.
     - **updated** = key in both, but the row's contents changed.
     - **deleted** = key **was** in the snapshot but is **gone now**. *(Keying the snapshot by a stable id is exactly what makes delete detection possible — a plain "list of rows" can't tell a delete from a re‑order, which is why the old design couldn't do deletes.)*
  4. For each change that matches the chosen `event`, call `run_workflow(wf, trigger_payload={ "event": "rowAdded|rowUpdated|rowDeleted", "row": <rowObject> })`. For a **delete**, `row` is the **last‑known values** from the snapshot.
  5. Save the new snapshot.
- **Front‑end panel:** spreadsheet picker (or paste the ID), tab name, event dropdown (incl. **Deleted**), key‑column box, interval.
- **Gotchas:** polling re‑reads the truth each time, so a missed change self‑corrects next pass (good). **Delete detection needs a stable `keyColumn`** — without one you can only do added/updated. Polling also can't always tell a *true* delete from a row that was filtered or moved out of the read range, so tell users to rely on a clean key column. Respect Google API quotas — don't poll hundreds of sheets every second.

---

### 7.9 Google Sheets — Append / Update 🔨 (Sprint 4)

- **What it does:** writes a row to a Google Sheet (append a new row, or update a matching one).
- **Type id:** `sheetsWrite`
- **Inputs/Outputs:** 1 in · 1 out.
- **Config fields:** `document`, `sheet`, `operation` (`append` / `appendOrUpdate`), `matchColumn` (for upsert, e.g. `Email`), `columns` (map of column → value, supports expressions).
- **Build it (backend):** use the Sheets API `values.append` (for append) or read‑match‑then‑`values.update` (for upsert). Uses the connected Google account token.
- **Front‑end panel:** document + tab pickers, operation dropdown, and a column‑mapping editor.
- **Example:** form trigger → this node appends `{ Name, Email, Date }`.
- **Gotchas:** the first row of the sheet is treated as headers; map by header name. Handle the "sheet/tab doesn't exist" error clearly.

---

### 7.10 Respond to Webhook 🔨 (Sprint 3)

- **What it does:** lets a **webhook‑triggered** workflow return a custom HTTP response (status + body) to whoever called it.
- **Type id:** `respondToWebhook`
- **Config fields:** `statusCode` (default 200), `bodyType` (`json` / `text`), `body` (supports expressions).
- **Front‑end panel:** a `statusCode` number box, a `bodyType` dropdown (`json` / `text`), and a `body` textarea (supports `{{ }}`).
- **Build it (backend):** only meaningful when the trigger's `responseMode = usingRespondNode`. The engine should **capture** this node's output and hand it back to the webhook route as the HTTP response.
- **Gotchas:** if a workflow uses this but wasn't started by a webhook (e.g. manual run), just ignore it.

---

### 7.11 Schedule / Cron trigger 🔨 (Sprint 6)

- **What it does:** starts the workflow on a timer — every N minutes, daily/weekly at a time, or a full cron expression. *(The #1 missing trigger vs n8n.)*
- **Type id:** `scheduleTrigger` · no input · 1 output (one item with the fire time).

**Catalog entry** (add to `NODE_CATALOG`):

```python
{'type': 'scheduleTrigger', 'group': 'trigger', 'name': 'Schedule', 'icon': 'clock',
 'implemented': False,
 'description': 'Run on a timer: every N minutes, daily/weekly at a time, or a cron expression.',
 'params': [
     {'key': 'mode', 'label': 'Run', 'type': 'select',
      'options': ['everyMinutes', 'everyHours', 'dailyAt', 'weeklyAt', 'cron'], 'default': 'dailyAt'},
     {'key': 'minutes', 'label': 'Every N minutes', 'type': 'number', 'default': 15,
      'showWhen': {'key': 'mode', 'in': ['everyMinutes']}},
     {'key': 'hours', 'label': 'Every N hours', 'type': 'number', 'default': 1,
      'showWhen': {'key': 'mode', 'in': ['everyHours']}},
     {'key': 'time', 'label': 'At time (HH:MM)', 'type': 'string', 'default': '09:00',
      'showWhen': {'key': 'mode', 'in': ['dailyAt', 'weeklyAt']}},
     {'key': 'weekday', 'label': 'Day', 'type': 'select',
      'options': ['mon','tue','wed','thu','fri','sat','sun'], 'default': 'mon',
      'showWhen': {'key': 'mode', 'in': ['weeklyAt']}},
     {'key': 'cron', 'label': 'Cron expression', 'type': 'string', 'default': '0 9 * * *',
      'showWhen': {'key': 'mode', 'in': ['cron']}},
     {'key': 'timezone', 'label': 'Timezone', 'type': 'string', 'default': 'Asia/Colombo'},
 ]},
```

**Build it (backend)** — one shared `BackgroundScheduler` turns each active schedule node into a job:

```python
# automations.py — Sprint 6 scheduler  (pip install apscheduler)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

scheduler = BackgroundScheduler(timezone='UTC')

def _trigger_from(p):
    """Turn a scheduleTrigger node's params into an APScheduler trigger."""
    tz = p.get('timezone') or 'Asia/Colombo'; mode = p.get('mode', 'dailyAt')
    if mode == 'everyMinutes': return IntervalTrigger(minutes=max(1, int(p.get('minutes', 15))))
    if mode == 'everyHours':   return IntervalTrigger(hours=max(1, int(p.get('hours', 1))))
    if mode == 'cron':         return CronTrigger.from_crontab(p.get('cron', '0 9 * * *'), timezone=tz)
    hh, mm = (p.get('time') or '09:00').split(':')
    if mode == 'weeklyAt':
        return CronTrigger(day_of_week=p.get('weekday', 'mon'), hour=int(hh), minute=int(mm), timezone=tz)
    return CronTrigger(hour=int(hh), minute=int(mm), timezone=tz)        # dailyAt

def _fire(wf_id, node_id):
    wf = _wf_load(wf_id)
    if wf and wf.get('active'):
        _save_run(run_workflow(wf, trigger_payload={'fired_at': _now_iso(), 'by': 'schedule'},
                               trigger_node_id=node_id))

def reload_schedule_jobs():
    """Re-register a job per ACTIVE workflow with a scheduleTrigger. Call at startup AND on save/activate."""
    for job in scheduler.get_jobs():
        if job.id.startswith('wf:'): job.remove()
    for meta in _wf_list():
        wf = _wf_load(meta['id'])
        if not (wf and wf.get('active')): continue
        for n in wf.get('nodes', []):
            if n.get('type') == 'scheduleTrigger':
                scheduler.add_job(_fire, _trigger_from(n.get('parameters', {})),
                                  id=f"wf:{wf['id']}:{n['id']}", args=[wf['id'], n['id']],
                                  replace_existing=True)

def start_scheduler():
    if not scheduler.running:        # single-start guard — don't double-start under gunicorn workers
        scheduler.start(); reload_schedule_jobs()
```

Call `start_scheduler()` once in `init_automations`, and `reload_schedule_jobs()` from the save/activate routes.

- **Front‑end panel:** the `mode` dropdown + the time/cron/interval fields (use `showWhen` so only the relevant field shows — §8.4).
- **Manual test:** pressing Run emits one item immediately. **Gotchas:** APScheduler must start **once** (the guard); the same scheduler also drives Sheets polling (§7.8) and durable Wait (§7.3) — share **one** instance.

---

### 7.12 Code node (Python) + stronger expressions 🔨 (Sprint 6)

- **What it does:** runs a small **Python** snippet to transform data — the escape hatch for anything no‑code nodes can't do. Pairs with the §6 stronger expressions (`{{ $node["X"].field }}`, `{{ $now }}`, `{{ $secrets.KEY }}`).
- **Type id:** `code` · 1 in · 1 out.

**Catalog entry:**

```python
{'type': 'code', 'group': 'action', 'name': 'Code (Python)', 'icon': 'code',
 'implemented': False,
 'description': 'Run a small sandboxed Python snippet to transform the data.',
 'params': [
     {'key': 'mode', 'label': 'Run', 'type': 'select',
      'options': ['eachItem', 'allItems'], 'default': 'eachItem'},
     {'key': 'code', 'label': 'Python', 'type': 'text',
      'default': '# `item` is the current row (a dict). Return a dict.\nreturn {**item, "ok": True}'},
 ]},
```

**Build it (backend) — SANDBOXED** (never `exec()` raw user code):

```python
# automations.py — Sprint 6 Code node  (pip install RestrictedPython)
from RestrictedPython import compile_restricted, safe_globals
from RestrictedPython.Guards import guarded_iter_unpack_sequence, safer_getattr

# Tight allow-list. NO __import__, open, eval, exec, file/network, dunders.
_SAFE_BUILTINS = {
    'len': len, 'range': range, 'min': min, 'max': max, 'sum': sum, 'sorted': sorted,
    'abs': abs, 'round': round, 'str': str, 'int': int, 'float': float, 'bool': bool,
    'list': list, 'dict': dict, 'set': set, 'tuple': tuple, 'enumerate': enumerate,
    'zip': zip, 'map': map, 'filter': filter, 'any': any, 'all': all, 'reversed': reversed,
}

def _run_user_code(src, local_vars):
    g = dict(safe_globals)
    g['__builtins__'] = _SAFE_BUILTINS
    g['_getattr_'] = safer_getattr
    g['_getitem_'] = lambda obj, k: obj[k]
    g['_getiter_'] = iter
    g['_iter_unpack_sequence_'] = guarded_iter_unpack_sequence
    g.update(local_vars)
    wrapped = 'def __fn():\n' + '\n'.join('    ' + ln for ln in src.splitlines())  # so `return` works
    exec(compile_restricted(wrapped, '<automation-code>', 'exec'), g)   # safe: restricted + allow-listed
    return g['__fn']()

def _exec_code(params, items, ctx=None):
    src = params.get('code') or 'return item'
    if params.get('mode') == 'allItems':
        out = _run_user_code(src, {'items': items, 'item': items[0] if items else {}})
        return [out if isinstance(out, list) else [out]]
    result = []
    for it in items:
        r = _run_user_code(src, {'item': it})
        result.append(r if isinstance(r, dict) else {'value': r})
    return [result]
# register: EXECUTORS['code'] = _exec_code
```

- **Front‑end panel:** a `mode` dropdown + a monospaced **code textarea** (a CodeMirror upgrade is optional).
- **Security (read this):** RestrictedPython blocks imports, attribute escapes and dangerous builtins — but **also** run with a wall‑clock **timeout** (run in a thread/subprocess you can kill) so an infinite loop can't hang a worker. *Stronger isolation:* run in a **subprocess** with `resource` CPU/memory limits. **Admin‑only** — never expose to non‑admins. A code error is a **hard error** (run marked failed, per §6).

---

### 7.13 Merge — join two branches 🔨 (Sprint 6)

- **What it does:** combines items from **two** branches (append, or merge by a matching key). Enables fan‑out/fan‑in.
- **Type id:** `merge` · **2 inputs** · 1 out.

```python
{'type': 'merge', 'group': 'logic', 'name': 'Merge', 'icon': 'git-merge',
 'implemented': False, 'inputs': 2,
 'description': 'Combine items from two branches (append, or merge by a key).',
 'params': [
     {'key': 'mode', 'label': 'Mode', 'type': 'select',
      'options': ['append', 'mergeByKey'], 'default': 'append'},
     {'key': 'key', 'label': 'Match key', 'type': 'string', 'default': 'id',
      'showWhen': {'key': 'mode', 'in': ['mergeByKey']}},
 ]},

def _exec_merge(params, inputs_by_port, ctx=None):
    a = inputs_by_port.get(0, [])      # items that arrived on input 0
    b = inputs_by_port.get(1, [])      # items that arrived on input 1
    if params.get('mode') == 'mergeByKey':
        key = params.get('key', 'id')
        idx = {(it or {}).get(key): it for it in b}
        return [[{**it, **(idx.get(it.get(key)) or {})} for it in a]]
    return [a + b]                     # append
```

**Engine change (multi‑input nodes):** a Merge must **wait for both branches** before running. The Sprint‑1 BFS runs a node as soon as it's reached; add a rule that a node declared `inputs: 2` is **deferred** until every upstream branch feeding it has finished, then run once with items grouped by input port:

```python
# in run_workflow: collect per-port inputs, and count how many edges feed each multi-input node
port_inputs = {}     # node_id -> { port_index: [items] }
pending_in  = {}     # node_id -> upstream edges still to deliver  (precompute expected_edges[node])

# when routing an output to a target whose input index is `idx`:
port_inputs.setdefault(tid, {}).setdefault(idx, []).extend(items_for_output)
if NODE_TYPES.get(target_type, {}).get('inputs', 1) >= 2:
    pending_in[tid] = pending_in.get(tid, expected_edges[tid]) - 1
    if pending_in[tid] <= 0:          # both branches in -> run it now
        queue.append(tid)
else:
    queue.append(tid)
# a multi-input executor receives port_inputs[tid] (a {port: items} dict) instead of a flat list
```

> Keep v1 simple: if one branch produced nothing, run Merge with whatever arrived. The Merge node renders **two input handles** (left side, top/bottom). Test: both branches full, one branch empty, and an IF feeding one side.

---

### 7.14 Loop / Split in Batches 🔨 (Sprint 6)

- **What it does:** tags items into **fixed‑size batches** (e.g. to respect an API rate limit).
- **Type id:** `loop` · **Honest scope:** our engine already runs most nodes **once per item**, so explicit loops are rarely needed. True n8n‑style **loop‑back cycles** are a bigger engine change and stay **out of scope** — this just batches items you can pace with a Wait.

```python
{'type': 'loop', 'group': 'logic', 'name': 'Loop (batches)', 'icon': 'repeat',
 'implemented': False,
 'description': 'Tag items into fixed-size batches (pair with Wait to pace API calls).',
 'params': [{'key': 'batchSize', 'label': 'Batch size', 'type': 'number', 'default': 10}]},

def _exec_loop(params, items, ctx=None):
    n = max(1, int(params.get('batchSize', 10)))
    return [[{**it, '_batch': i // n, '_batchIndex': i} for i, it in enumerate(items)]]
```

- **Gotchas:** of the four gaps this is the weakest fit for a single‑pass engine — prioritise **Merge + Sub‑workflow + per‑item execution + Wait**, which together cover most real "loop" needs (e.g. "for each row, call the API, pause 2s").

---

### 7.15 Execute Sub‑workflow 🔨 (Sprint 6)

- **What it does:** runs **another saved workflow** and returns its output — reuse shared logic (e.g. a "send our standard Slack alert" workflow) from many places.
- **Type id:** `executeSubWorkflow` · 1 in · 1 out.

```python
{'type': 'executeSubWorkflow', 'group': 'action', 'name': 'Execute Sub-workflow',
 'icon': 'workflow', 'implemented': False,
 'description': 'Run another saved workflow and return its output. Reuse shared logic.',
 'params': [{'key': 'workflowId', 'label': 'Workflow', 'type': 'select', 'options': [], 'default': ''}]},

def _exec_subworkflow(params, items, ctx=None, _depth=0):
    if _depth > 5:
        raise RuntimeError('Sub-workflow nesting too deep (possible loop).')
    sub = _wf_load(params.get('workflowId'))
    if not sub:
        raise RuntimeError(f"Sub-workflow not found: {params.get('workflowId')}")
    run = run_workflow(sub, trigger_payload={'items': items})
    if run['status'] == 'error':
        raise RuntimeError(f"Sub-workflow failed: {run.get('error')}")
    last = run['node_runs'][-1] if run['node_runs'] else {}
    return [last.get('output') or []]      # the sub-workflow's final output
```

- **Front‑end:** the `workflowId` dropdown is filled from `GET /api/automations`.
- **Gotchas:** **cap recursion depth** (above) so a workflow calling itself can't loop forever; the sub‑workflow's `manualTrigger` receives `{'items': [...]}` as its payload.

---

### Node summary table

| Node | Type id | Sprint | Outputs |
|---|---|---|---|
| Manual / Test | `manualTrigger` | 1 ✅ | 1 |
| HTTP Request | `httpRequest` | 1 ✅ | 1 |
| Wait | `wait` | 1 ✅ (durable: 5) | 1 |
| IF | `if` | 1 ✅ | 2 (true/false) |
| Edit Fields (Set) | `set` | 1 ✅ | 1 |
| Webhook | `webhookTrigger` | 3 🔨 | 1 |
| Form submission | `formTrigger` | 3 🔨 | 1 |
| Respond to Webhook | `respondToWebhook` | 3 🔨 | — |
| Google Sheets trigger | `sheetsTrigger` | 4 🔨 | 1 |
| Google Sheets write | `sheetsWrite` | 4 🔨 | 1 |
| Schedule / Cron | `scheduleTrigger` | 6 🔨 | 1 |
| Code (Python) | `code` | 6 🔨 | 1 |
| Merge | `merge` | 6 🔨 | 1 *(2 inputs)* |
| Loop (batches) | `loop` | 6 🔨 | 1 |
| Execute Sub‑workflow | `executeSubWorkflow` | 6 🔨 | 1 |

---

## 8. The visual canvas (front‑end)

**Goal:** a page at `/automations` where the user drags nodes, connects them, configures them, and presses Run. Built with **React Flow loaded from a CDN** — **copy the exact pattern already used in `static/flows.js`** (it loads React + React Flow from `esm.sh`, no build step).

> 🎨 **Read [§8.6 Design system & icons](#86-design-system--icons-modern--minimal) BEFORE building any UI.** The look must be modern and minimal, and icons must be from a clean line‑icon set — no ugly clip‑art.

### 8.1 Page layout (`templates/automations.html`)

Three columns inside the existing admin shell (`{% extends "base.html" %}`):

```
┌──────────┬──────────────────────────────┬───────────────┐
│ PALETTE  │            CANVAS             │  CONFIG PANEL  │
│ (drag    │   (React Flow: nodes+edges)  │  (fields for   │
│  nodes   │                              │   the selected │
│  from    │                              │   node)        │
│  here)   │                              │                │
├──────────┴──────────────────────────────┴───────────────┤
│  [Save]   [▶ Run]            RUN LOG (per-node results)  │
└──────────────────────────────────────────────────────────┘
```

### 8.2 Build steps (in order)

1. **Load the catalog:** `GET /api/automations/_node-catalog` → render the **palette** (group by trigger / action / logic; show name + icon).
2. **Mount React Flow** (copy from `flows.js` lines ~300–340): `import React from "https://esm.sh/react@18"`, `react-dom@18/client`, `reactflow@11`, and the CSS link.
3. **Drag‑and‑drop add:** use HTML5 drag from the palette; on drop, use React Flow's `screenToFlowPosition` to place a new node at the cursor. (React Flow has an official "drag and drop" example — follow it.)
4. **Connect nodes:** React Flow's `onConnect` creates an edge. Convert edges ↔ our `connections` JSON on save/load (edge `source/target` → `connections[source].main[outputIndex]`).
5. **Select a node** → fill the **config panel** from the catalog's `params` list, rendering one control per field using the **field‑type → control mapping in §8.4**, and honouring each param's `showWhen` (conditional visibility).
6. **Save:** `PUT /api/automations/<id>` with the full JSON (nodes + connections). Convert React Flow's node/edge arrays into our schema first.
7. **Run:** `POST /api/automations/<id>/run` → show the returned **run log** at the bottom (each node: ✅/❌, items in/out, a sample of the output, any error).
8. **Add the sidebar link:** in `templates/base.html`, the nav list already has a `('flows', …)` entry — add `('automations','/automations','workflow','Automations','admin')` right below it.

### 8.3 Converting React Flow ↔ our JSON

- **Node:** React Flow `{ id, type, position, data }` ↔ our `{ id, name, type, position, parameters }`. Keep our `name`/`parameters` inside React Flow's `data`.
- **Edge:** React Flow `{ source, target, sourceHandle }` → our connection. `sourceHandle` tells you the **output index** (use `"0"`/`"1"` for IF true/false).
- **Handles:** a normal node renders **one input handle (left) + one output handle (right)**. The **IF** node renders **two output handles**: `"0"` (true, top) and `"1"` (false, bottom). Triggers have **no input handle**.
- **Deleting:** wire React Flow's `onNodesDelete` / `onEdgesDelete` so the user can remove a node or a line — and drop the matching entry from `connections` on save.

### 8.4 The node‑catalog `params` schema (the contract that builds every config panel)

The config panel is **generated from data**, not hand‑coded per node. `GET /api/automations/_node-catalog` returns each node with a `params` list. **Each param object has this shape:**

```json
{
  "key": "method",              // saved into node.parameters[key]
  "label": "Method",            // shown to the user
  "control": "select",          // which widget to render (see table below)
  "default": "GET",
  "options": ["GET", "POST"],   // only for "select"
  "help": "optional hint text",
  "showWhen": { "key": "bodyType", "in": ["json", "raw"] }  // optional: only show this field when another field has one of these values
}
```

**Field‑type → control mapping** — build ONE small renderer that switches on `control`:

| `control` | Widget to render | Saves as |
|---|---|---|
| `string` | single‑line text box | string |
| `text` | multi‑line textarea | string |
| `number` | number input | number |
| `select` | dropdown of `options` | string |
| `bool` | checkbox | true / false |
| `json` | textarea validated as JSON (e.g. HTTP headers) | object |
| `keyvalue` | repeatable "name / value" rows | list of `{name, value}` |
| `fieldlist` | repeatable row editor (e.g. Form fields: label / type / required) | list of objects |

Rules: render fields **in order**; **hide** a field whose `showWhen` isn't met (e.g. HTTP's `body` is hidden while `bodyType = none`); write each value to `node.parameters[key]`. This one renderer powers **every** node's panel — to add a node you add a catalog entry, **no new UI code**.

### 8.5 Acceptance for Sprint 2

- You can open the page, drag a **Manual → Set → HTTP → IF** chain, connect them, configure each, **Save**, reload (the diagram persists), **Run**, and **see the run log** — all in the browser.
- **Every** node type in the catalog renders its panel, and **every** `control` type from §8.4 appears at least once and saves correctly.
- You can **delete** a node and an edge, and the saved JSON updates.

### 8.6 Design system & icons (modern & minimal)

> **Read this before building any UI.** The bar is a clean, modern, minimalist look — think **Linear / Vercel**. No clutter, no cheap clip‑art icons.

**Reuse the app's EXISTING design system — do not invent a new one.** `templates/base.html` already defines a polished system; match it exactly:

- **Font:** Inter (already loaded).
- **Colours:** use the existing CSS variables / Tailwind tokens — `bg`, `card`, `border`, `muted-fg`, `accent`, `primary`, plus status colours `emerald` (success), `rose` (error), `amber` (warning), `sky` (info). **Never hard‑code hex colours.**
- **Dark mode:** must work in both themes (the tokens handle it automatically — test both).
- **Spacing & shape:** generous whitespace, `rounded-md` / `rounded-xl`, subtle 1px borders (`border-border`), the `shadow-subtle` shadow, small quiet secondary text (`text-xs` / `text-[11px]`).
- **Motion:** subtle only — `transition-colors`, gentle hovers. No bouncy animations.

**Layout principles (minimalist):**

- The **canvas is the hero** — keep the palette and config panel quiet (muted backgrounds, thin borders).
- **One primary action per area** (`Run` / `Save` use `bg-primary`); everything else is a quiet ghost/outline button.
- Empty states = one centred line + one icon, like the existing pages.

**Icons — use a modern line‑icon set, never random images.**

The app **already uses [Lucide](https://lucide.dev)** (loaded in `base.html`; rendered with `<i data-lucide="name"></i>` then `lucide.createIcons()`). Lucide is a clean, modern, consistent line set — **use it for all UI and node icons.** This matches the rest of the panel and needs **zero new files**.

**Node → Lucide icon map** (all are real Lucide names):

| Node / control | `data-lucide` |
|---|---|
| Manual / Test | `mouse-pointer-click` |
| Webhook | `webhook` |
| Form | `clipboard-list` |
| Google Sheets (trigger / write) | `sheet` (or the brand SVG below) |
| HTTP Request | `globe` |
| Wait | `timer` |
| IF | `git-branch` |
| Set / Edit Fields | `pencil` |
| Respond to Webhook | `reply` |
| History / versions | `history` |
| Export / Import | `download` / `upload` |
| Run / Save | `play` / `save` |

**Brand logos Lucide doesn't have** (the real Google Sheets / Google logos, for the "Connect Google" button and the Sheets nodes) go in a dedicated folder:

```
pbx-monitor/static/icons/automations/
├── README.md          # manifest: each icon + official source + licence
├── google.svg
├── google-sheets.svg
└── google-drive.svg
```

Rules for these SVGs: a **single clean `<svg>`** (no embedded PNG), a proper `viewBox`, sized with CSS (`w-4 h-4`), and `currentColor` for monochrome ones so they theme correctly. Reference with `<img src="/static/icons/automations/google-sheets.svg" class="w-4 h-4">`.

**Where to get clean SVGs (developer):** use the brand's **official** brand/press kit, or a curated open set like **[Simple Icons](https://simpleicons.org)** (clean single‑path SVGs, CC0). Download the exact files into the folder above and list each in `README.md` with its source URL. **Do not** screenshot logos or grab random PNGs.

> *A starter `pbx-monitor/static/icons/automations/` folder + `README.md` manifest has been created in the repo — fill it with the official SVGs.*

**Acceptance (AUTO‑209):** the Automations pages are visually **indistinguishable in style** from the existing panel (same font, tokens, dark mode); every node and button uses a Lucide icon or a vetted brand SVG from the folder; there are **no raster / clip‑art icons** anywhere.

---

## 9. Triggers deep‑dive

A trigger is "how the workflow starts". Each has its own entry point:

| Trigger | Entry point | Who calls it |
|---|---|---|
| Manual | the **Run** button | the admin, for testing |
| Webhook | a public URL `/api/automations/hook/<token>` | any external system |
| Form | a public page `/automations/form/<token>` | a human filling a form |
| Google Sheets | a **background scheduler** (every minute) | our own server, polling Google |

**Active vs inactive:** a workflow has an `active` flag. **Only active workflows** are reachable by webhook/form/scheduler. While building, keep it inactive; flip it on to go live. (The Manual run works regardless.)

**The scheduler (Sprint 4–5):** add **APScheduler** (`pip install apscheduler`). One background scheduler started inside the Flask process runs a "poll Google Sheets" job every minute and (Sprint 5) resumes durable Waits. Start it once when the app boots; guard it so it doesn't start twice under a multi‑worker server.

---

## 10. Google integration — full walkthrough

This is what lets a workflow read/write the team's Google Sheets. Because we are **internal‑only**, the setup is the **easy path** (no Google verification review).

### 10.1 The mental model (OAuth in plain English)

1. Our app sends the user to Google's "Allow access?" screen.
2. The user clicks **Allow**.
3. Google sends back a one‑time **code**.
4. Our server swaps that code for two keys: a short‑lived **access token** (~1 hour) and a long‑lived **refresh token** (used to get new access tokens forever).
5. We **store the refresh token (encrypted)** and use it whenever a node touches Sheets.

### 10.2 Get the Client ID & Client Secret (click‑by‑click)

> Do this **once**, signed in with a **Google Workspace admin** account for your company (so "Internal" is available).

1. Go to **https://console.cloud.google.com** and sign in with the **company Workspace** account.
2. Top bar → **project dropdown** → **New Project**. Name it `naxter-automations`. Make sure **Organization** = your company (not "No organization"). Click **Create**, then select it.
3. Left menu → **APIs & Services → Library**. Search **"Google Sheets API"** → **Enable**. (If you'll also let users *pick/list* spreadsheets, also enable **"Google Drive API"**.)
4. Left menu → **APIs & Services → OAuth consent screen** (newer console calls this **"Google Auth platform → Branding / Audience"**).
   - **User type / Audience:** choose **Internal**. ← the most important click. (Internal = only your company's Google accounts can connect; **no Google verification review**, no "unverified app" warning, no 7‑day token expiry.)
   - Fill **App name** (e.g. "Naxter Automations"), **User support email**, **Developer contact email**. Save.
5. **Scopes:** add `https://www.googleapis.com/auth/spreadsheets` (read **and** write Sheets). Add `https://www.googleapis.com/auth/drive.file` **only if** you let users create/pick files. Save. *(For Internal apps, scopes aren't shown on the consent screen and don't trigger review.)*
6. Left menu → **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - **Application type:** **Web application**.
   - **Name:** `naxter-automations-web`.
   - **Authorized redirect URIs → Add URI:** paste the **exact** callback URL your app uses, e.g. `https://monitor.easmoney.me/oauth/google/callback` (and `http://localhost:5051/oauth/google/callback` for local testing). It must match **character‑for‑character** what the app sends.
7. Click **Create**. A popup shows your **Client ID** and **Client Secret**. **Copy both.**
8. Put them in our config (see §11). **Never** commit them to git or send them to the browser.

> 🔑 **That popup is the answer to "how do I get the Client ID and Secret."** Client ID looks like `1234567890-abc...apps.googleusercontent.com`; the Secret looks like `GOCSPX-....`.

### 10.3 The connect flow to build (Sprint 4)

1. **"Connect Google" button** in an Automations → Settings area → hits `GET /oauth/google/start`.
2. That route makes a random `state`, saves it in the session, and **redirects** the browser to Google's auth URL with: `client_id`, `redirect_uri`, `response_type=code`, `scope=https://www.googleapis.com/auth/spreadsheets` (use the **full literal** scope string — for read‑only use `https://www.googleapis.com/auth/spreadsheets.readonly` instead), **`access_type=offline`** (required to get a refresh token), **`prompt=consent`**, and `state`.
3. Google redirects back to `GET /oauth/google/callback?code=...&state=...`. Verify `state`, then **POST** the `code` to `https://oauth2.googleapis.com/token` with your client id/secret to get `access_token` + `refresh_token`.
4. **Encrypt and store** the refresh token (see §11 for the key) in `instance/automations-credentials.json` along with the connected Google email.
5. Show "Connected as alice@company.com — Disconnect". Disconnect deletes the token and calls Google's revoke endpoint.

**Recommended libraries** (these handle refresh for you, fewer bugs): `pip install google-auth google-auth-oauthlib google-api-python-client`. The rest of the app (HTTP node) stays on stdlib; only the Google parts use these.

### 10.4 Refreshing tokens

Access tokens die after ~1 hour. Before a Sheets call, if the access token is expired, POST to the token endpoint with `grant_type=refresh_token` + the stored refresh token → get a fresh access token. (`google-auth` does this automatically if you use its `Credentials` object.) If a refresh ever returns `invalid_grant`, the connection is broken → ask the user to reconnect.

---

## 11. Environment variables & secrets (complete list)

Our app reads secrets two ways: from **`pbx-monitor/instance/*.json`** files (like `auth.json`, `ami.json`, `aws.json`) and from **`/opt/sampath-ai/.env`** (the `GEMINI_API_KEY`). For this feature, use a new **`instance/google.json`** (fits the existing pattern) plus a few optional env vars.

### 11.1 New file: `pbx-monitor/instance/google.json`

```json
{
  "client_id":     "1234567890-abc...apps.googleusercontent.com",
  "client_secret": "GOCSPX-xxxxxxxxxxxxxxxxxxxx",
  "redirect_uri":  "https://monitor.easmoney.me/oauth/google/callback",
  "encryption_key": "PUT_A_FERNET_KEY_HERE="
}
```

| Key | What it is | Where to get it |
|---|---|---|
| `client_id` | Google OAuth Client ID | §10.2 step 7 |
| `client_secret` | Google OAuth Client Secret | §10.2 step 7 |
| `redirect_uri` | the callback URL | must equal the one you put in Google (§10.2 step 6) |
| `encryption_key` | a key to encrypt stored refresh tokens | generate it (see below) |

**Generate the encryption key** (run once on the server):

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

(Install once: `pip install cryptography`.) Keep this key secret and **back it up** — if you lose it, all stored Google tokens become unreadable and users must reconnect.

### 11.2 Optional environment variables (have sensible defaults)

| Variable | Default | Purpose |
|---|---|---|
| `AUTOMATIONS_DIR` | `/var/lib/sampath-ai/automations` | where workflow JSON files live |
| `AUTOMATIONS_RUNS_DIR` | `/var/lib/sampath-ai/automations-runs` | where run logs live |
| `AUTOMATIONS_BASE_URL` | `https://monitor.easmoney.me` | used to build webhook & form URLs and the OAuth redirect |

> The app is reachable at **`monitor.easmoney.me`** (via Cloudflare tunnel — see the repo `README.txt`). Use that as the base URL in production.

### 11.3 New Python packages to install on the server

```bash
pip install cryptography apscheduler google-auth google-auth-oauthlib google-api-python-client
```

(Record these in a new `pbx-monitor/requirements.txt` so deploys are repeatable. The HTTP node needs **nothing** — it uses the standard library.)

### 11.4 File permissions

`instance/google.json` and `instance/automations-credentials.json` hold secrets → `chmod 0640`, owned by the app user (the deploy installs other instance files the same way).

### 11.5 Named secret store (encrypted — referenced by name) 🔨 (Sprint 6)

So API keys stop living **inside** workflow JSON files, add a tiny encrypted key/value store. Users save a secret **by name**, then reference it anywhere as `{{ $secrets.NAME }}` — resolved **server‑side at run time** (the value never reaches the browser or an exported file).

```python
# automations_secrets.py — Sprint 6  (reuses the Fernet key already in instance/google.json)
import json
from pathlib import Path
from cryptography.fernet import Fernet

_SECRETS_FILE = Path('/opt/pbx-monitor/instance/automations-secrets.json')
_GOOGLE_FILE  = Path('/opt/pbx-monitor/instance/google.json')

def _fernet():
    key = json.loads(_GOOGLE_FILE.read_text())['encryption_key']    # same key as §11.1
    return Fernet(key.encode())

def _load():
    return json.loads(_SECRETS_FILE.read_text()) if _SECRETS_FILE.exists() else {}

def set_secret(name, value):
    data = _load()
    data[name] = _fernet().encrypt(value.encode()).decode()         # store ciphertext only
    tmp = _SECRETS_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2)); tmp.chmod(0o640); tmp.replace(_SECRETS_FILE)

def get_secret(name):
    tok = _load().get(name)
    return _fernet().decrypt(tok.encode()).decode() if tok else None

def list_secret_names():
    return sorted(_load().keys())           # NAMES only — never return values to the browser
```

**Routes** (admin‑only), inside `init_automations`:

```python
    @app.route('/api/automations/_secrets')
    @login_required
    @perm_required('admin')
    def _secrets_list():
        from automations_secrets import list_secret_names
        return jsonify({'names': list_secret_names()})

    @app.route('/api/automations/_secrets', methods=['POST'])
    @login_required
    @perm_required('admin')
    def _secrets_set():
        from automations_secrets import set_secret
        b = request.get_json(silent=True) or {}
        if not b.get('name'):
            return jsonify({'error': 'name required'}), 400
        set_secret(b['name'], b.get('value', ''))
        return jsonify({'ok': True})
```

**Use it:** in an HTTP node header → `Authorization: Bearer {{ $secrets.SLACK_TOKEN }}` (the §6 engine upgrade resolves `$secrets.*` via `get_secret`).

- **UI:** a small **Secrets** settings page (admin) listing names with **Add** / **Delete**; values are **write‑only** (set a new value, never read the old one).
- **Security:** encrypted at rest, never logged, never returned by the API, and **stripped from exported workflows** (exports keep the `{{ $secrets.X }}` reference, not the value). Reuses the Fernet key in `instance/google.json`.

---

## 12. Security checklist

| Risk | What to do |
|---|---|
| **Anyone editing automations** | Builder pages & APIs stay `@perm_required('admin')`. ✅ already enforced. |
| **Webhook/form are public** | They must be public to receive calls, but protect with a long random `token` + optional **secret header**; consider rate‑limiting. |
| **Stored Google tokens** | **Encrypt** the refresh token at rest (Fernet key in §11). Never log tokens. `chmod 0640`. |
| **SSRF (HTTP node hits internal IPs)** | Internal‑only tool, so lower risk — but keep the 20s timeout & 1 MB cap, and document it. Optionally block `localhost`/private ranges. |
| **Expressions** | Our `{{ }}` is a **safe find‑replace** — never `eval()` user input. ✅ by design. |
| **Secrets in git** | `instance/*.json` and `.env` are **git‑ignored** (per repo `README.txt`). Keep it that way. |
| **Scheduler double‑start** | Under a multi‑worker server, ensure the background scheduler starts **once** (guard with a lock/flag). |

---

## 13. Storage & file layout

```
pbx-monitor/
├── app.py                         # +2 lines wire in automations (done)
├── automations.py                 # the module (Sprint 1 done; you extend it)
├── templates/
│   └── automations.html           # Sprint 2 (new)
├── static/
│   └── automations.js             # Sprint 2 (new)
└── instance/
    ├── google.json                # Sprint 4 — OAuth client + encryption key (secret)
    ├── automations-credentials.json  # Sprint 4 — encrypted Google tokens (secret)
    └── automations-secrets.json    # Sprint 6 — encrypted named secrets, used via {{ $secrets.X }}

/var/lib/sampath-ai/
├── automations/<id>.json          # the saved workflows
├── automations-runs/<id>/<run>.json  # run logs (keep last 25 per workflow)
├── automations-versions/<id>/<version>.json  # saved versions for history & rollback (keep last ~30)
├── automations-state/<id>/stats.json  # run counters: total / succeeded / failed / last_run_at / last_status
└── automations-state/             # Sprint 4–5: sheet snapshots (keyed by keyColumn), durable-wait state, webhook index
```

Every write uses the **atomic `.tmp` → rename** pattern already in the code (so a crash never leaves a half‑written file). Mirror all edits from `pbx-monitor/` into `staging/` (source of truth).

---

## 14. API reference

All under `@login_required` + `@perm_required('admin')` **except** the public webhook/form routes. (Sprint‑1 routes already exist.)

| Method & path | Purpose | Sprint |
|---|---|---|
| `GET /automations` | the builder page | 2 |
| `GET /api/automations/_node-catalog` | node types + their fields (drives palette/panel) | 1 ✅ |
| `GET /api/automations` | list workflows (incl. run stats: executed N times, ok/fail, last run) | 1 ✅ (stats: 5) |
| `POST /api/automations` | create (from name) | 1 ✅ |
| `GET /api/automations/<id>` | load one | 1 ✅ |
| `PUT /api/automations/<id>` | save (validates) | 1 ✅ |
| `DELETE /api/automations/<id>` | delete | 1 ✅ |
| `POST /api/automations/<id>/run` | run now (manual), returns run log | 1 ✅ |
| `GET /api/automations/<id>/runs` | recent run logs | 1 ✅ |
| `GET /api/automations/<id>/versions` | list saved versions | 2 |
| `GET /api/automations/<id>/versions/<v>` | load one past version (read‑only) | 2 |
| `POST /api/automations/<id>/restore/<v>` | restore a version (snapshots current first) | 2 |
| `GET /api/automations/<id>/export` | download the workflow as a JSON file | 2 |
| `POST /api/automations/import` | create a workflow from an uploaded JSON | 2 |
| `POST /api/automations/<id>/activate` | turn active on/off | 3 |
| `GET/POST /api/automations/hook/<token>` | **public** webhook entry | 3 |
| `GET/POST /automations/form/<token>` | **public** hosted form | 3 |
| `GET /oauth/google/start` | begin Google connect | 4 |
| `GET /oauth/google/callback` | finish Google connect | 4 |
| `GET /api/automations/_secrets` | list secret **names** (never values) | 6 |
| `POST /api/automations/_secrets` | add / update a named secret | 6 |
| `DELETE /api/automations/_secrets/<name>` | delete a secret | 6 |

---

## 15. Sprint breakdown

Total realistic size: **~2.5–3 months** for one developer. Five sprints. Each ticket has an **acceptance test** (how you prove it's done).

### Sprint 1 — Foundation ✅ (DONE — for reference)
**Goal:** the engine + data model + API exist and are tested.
- `automations.py`: data model, `run_workflow` engine, executors for manual/set/if/http/wait, expressions, CRUD + run routes, atomic storage.
- **Acceptance:** ✅ unit tests pass (expressions, IF routing, branch pruning, wait cap, save/load, all 9 routes register).

### Sprint 2 — Visual canvas, design, versioning & import/export (≈ 2 weeks)
**Goal:** the user can build, save, run, **version**, and **import/export** a workflow in the browser — with a clean, modern UI.
- **AUTO‑201** `automations.html` 3‑column layout inside `base.html`. *Accept:* page loads, sidebar link works.
- **AUTO‑202** Load + render the palette from `_node-catalog`. *Accept:* all node types appear, grouped.
- **AUTO‑203** Mount React Flow from CDN (copy `flows.js`). *Accept:* empty canvas pans/zooms.
- **AUTO‑204** Drag‑to‑add + connect + **delete** nodes/edges. *Accept:* can build a 4‑node chain visually and delete a node/edge (saved JSON updates).
- **AUTO‑205** Config panel driven by the **§8.4 `params` schema**. *Accept:* **every** node type renders its panel and **every** control type (string/text/number/select/bool/json/keyvalue/fieldlist) renders, respects `showWhen`, and saves.
- **AUTO‑206** Save (PUT) + reload keeps the diagram. *Accept:* refresh shows the same graph.
- **AUTO‑207** Run button → run‑log panel. *Accept:* Manual→Set→HTTP runs and shows per‑node results; a failing node shows ❌ + its error.
- **AUTO‑208** Workflow **run stats** (executed N times). *Accept:* after running a workflow 3 times with 1 failure, the list/header shows "Executed 3 times · 2 ok / 1 failed · last run …".
- **AUTO‑209** Design & icons pass (per §8.6): match the existing design tokens + Lucide; create & fill `static/icons/automations/`. *Accept:* pages match the panel's style in light **and** dark; every icon is Lucide or a vetted brand SVG; no clip‑art anywhere.
- **AUTO‑210** Version history: snapshot on save + `version` counter + **History** panel + **View** + **Restore**. *Accept:* saving 3 times yields v1/v2/v3; Restore brings back an older diagram and itself creates a new version (so it's undoable).
- **AUTO‑211** Export & Import JSON. *Accept:* Export downloads a `.json`; importing it creates a new **inactive** workflow that runs identically; a malformed or oversized file is rejected with a clear message.

### Sprint 3 — Real triggers (≈ 1–2 weeks)
**Goal:** workflows start from webhooks and forms.
- **AUTO‑301** `active` flag + activate route + UI toggle.
- **AUTO‑302** Webhook node + public hook route + secret‑header auth + URL display.
- **AUTO‑303** Form node + public hosted form page + submit handling.
- **AUTO‑304** Respond‑to‑Webhook node + `responseMode` handling.
- *Accept:* calling the webhook URL (curl **and** an HTML `<form>` / `fetch` from an external page) runs the workflow; submitting the hosted form runs it; webhook can return a custom response.

### Sprint 4 — Google + Sheets (≈ 2–3 weeks)
**Goal:** connect Google and read/write Sheets; "fire on change" via polling.
- **AUTO‑401** Google Cloud project + OAuth client (the §10.2 walkthrough). *Accept:* you hold a Client ID/Secret.
- **AUTO‑402** OAuth connect/callback routes + **encrypted** token storage + Connect/Disconnect UI.
- **AUTO‑403** Token refresh helper (use `google-auth`).
- **AUTO‑404** Sheets **write** node (append / upsert).
- **AUTO‑405** Sheets **trigger** via the polling scheduler (1‑min; snapshot keyed by `keyColumn`; detects **added / updated / deleted**).
- *Accept:* "Connected as …" shows; a workflow appends a row; **adding** a row fires `rowAdded`, **editing** a row fires `rowUpdated`, and **deleting** a row fires `rowDeleted` — each within ~1 minute.

### Sprint 5 — Durable & history (≈ 1–2 weeks)
**Goal:** reliability + visibility.
- **AUTO‑501** APScheduler running in‑process (single‑start guard).
- **AUTO‑502** **Durable Wait** (save state, resume later — survives restart).
- **AUTO‑503** Error handling + retries per node (per §6 "Error handling"). *Accept:* a node that throws marks the run **failed**, the run log shows the error + the failing node id, the node is flagged **red on the canvas**, and a node with `retryOnFail` + `maxRetries=2` is attempted 3 times before failing.
- **AUTO‑504** Run‑history screen (list past runs — **including webhook/form/scheduler runs** — open one, see each node + any error).
- *Accept:* a 10‑minute Wait survives an app restart and still continues; run history (including failed runs and their errors) is browsable.

### Sprint 6 — Parity essentials (≈ 2–3 weeks)
**Goal:** the four "real gaps" vs n8n that make it genuinely useful day‑to‑day. Every ticket ships with example code in §6 / §7 / §11.

- **AUTO‑601** Schedule / Cron trigger — expose APScheduler as a trigger node (§7.11). *Accept:* "every 2 minutes" runs on its own; "daily at 09:00" and a cron expression both fire; jobs reload on save/activate; manual Run still works.
- **AUTO‑602** Stronger expressions — engine context (§6): `{{ $node["Name"].field }}`, `{{ $now }}`, `{{ $secrets.X }}`. *Accept:* a later node reads an earlier node's output and a secret; old `{{ $json.x }}` still works.
- **AUTO‑603** Code (Python) node — sandboxed with RestrictedPython + allow‑list + timeout (§7.12). *Accept:* a code node transforms items; `import` / `open` / `exec` are blocked; an error marks the run failed; admin‑only.
- **AUTO‑604** Named secret store — encrypted + a Secrets settings UI (§11.5). *Accept:* add a secret by name; use it via `{{ $secrets.X }}` in a header; the value never appears in the API list, run logs, or an exported workflow.
- **AUTO‑605** Merge + Execute Sub‑workflow nodes, plus basic Loop/batch (§7.13–7.15). *Accept:* two branches merge by key; a sub‑workflow runs and returns data; sub‑workflow recursion is depth‑capped.

> These four are cheap relative to their value — the APScheduler dependency and the Fernet encryption key are **already** planned for Sprints 4–5, so 601 and 604 mostly reuse existing pieces.

---

## 16. How to deploy

We **edit in `staging/`**, then run an install script that copies to `/opt/pbx-monitor` and restarts the service. Follow the existing `install-flows-v2.sh` pattern. Create **`staging/install-automations.sh`** that:

1. **Pre‑flight:** `python3 -c "import ast; ast.parse(open('app.py').read()); ast.parse(open('automations.py').read())"` and `node --check static/automations.js`.
2. **Back up** the current files as `*.bak-automations-<timestamp>`.
3. **Copy** `app.py`, `automations.py`, `templates/automations.html`, `static/automations.js` into `/opt/pbx-monitor/...` with the right owner/permissions (match how `install-dashboards.sh` does `install -o asterisk -g asterisk -m 0644`).
4. **Install** any new Python packages (§11.3) into the app's environment.
5. **Create** `instance/google.json` from your values (don't overwrite if it exists).
6. **Restart:** `systemctl restart pbx-monitor` and check `journalctl -u pbx-monitor` for the `[automations]` line (it prints if the module failed to load).

> Always run the install script with `sudo`, and keep it **idempotent** (safe to run twice) like the others.

---

## 17. Definition of Done

A sprint is "Done" when **all** of these are true:

- [ ] Every ticket's **acceptance test** passes.
- [ ] `app.py` **and** `automations.py` pass `ast.parse` (no syntax errors).
- [ ] `static/automations.js` passes `node --check`.
- [ ] Changes are mirrored into **`staging/`** (source of truth) and the **install script** updated.
- [ ] Secrets are in `instance/*.json` (git‑ignored), **never** in code or the browser.
- [ ] Admin‑only routes still require `@perm_required('admin')`; public routes have a token/secret.
- [ ] **Errored runs are clearly surfaced** (red node + error message in the run log/history), and each workflow shows its **run stats** ("executed N times").
- [ ] Saving creates a **restorable version**; **Export → Import** round‑trips a workflow; the UI matches the app's design system in **light and dark**.
- [ ] A short note added to the repo `README.txt` describing the new `/automations` feature and its install script.
- [ ] Manually verified on the server: build → save → run → (trigger) end‑to‑end works.

---

## 18. Risks & out‑of‑scope

**Risks**
- **Durable Wait is genuinely hard** (state must survive restarts). Budget extra time for AUTO‑502.
- **Google polling quotas** — don't poll too many sheets too often.
- **No real database** — JSON files are fine for low internal volume; if usage grows, plan a move to SQLite.
- **Single Flask process assumptions** — the in‑process scheduler must start once; revisit if the app is scaled to multiple workers.

**Now IN scope (added in Sprint 6 — see §15):** Schedule/Cron trigger, Code node + stronger expressions, a named secret store, and Merge / Loop / Sub‑workflow nodes. *(These were the four "real gaps" vs n8n.)*

**Out of scope (v1)** — say "not yet" to these so the sprint stays finishable:
- A library of 400+ integrations (we ship a focused node set + the universal HTTP node). Add connectors later, one at a time.
- Multiple users editing the same workflow at once.
- True n8n‑style **loop‑back cycles** (the Sprint‑6 Loop node does fixed‑size batching only — see §7.14).
- A full per‑service **OAuth credential catalog** (we ship Google OAuth + a generic named‑secret store; other services use a secret in a header).
- **AI / LLM nodes** — a conscious decision for now. If the team will ever want an "ask an LLM / classify this" step, add one minimal LLM node to the backlog; otherwise this stays out.
- Queue‑mode / horizontal scaling, SSO / RBAC / projects / git promotion, the template marketplace, and a community‑node plugin system — genuinely unnecessary for one internal team.

---

## Appendix A — example workflows

**A1. "New lead → Slack + Sheet"** (uses Form, HTTP, Sheets write)
`Form trigger (Name, Email)` → `HTTP Request (POST Slack webhook, body uses {{ $json.Email }})` → `Google Sheets append (Name, Email, Date)`.

**A2. "Watch a sheet → call an API"** (uses Sheets trigger, IF, HTTP)
`Google Sheets trigger (rowAdded)` → `IF ({{ $json.row.Status }} equal "approved")` → **true:** `HTTP Request (PUT to your API)` / **false:** *(nothing)*.

**A3. "Webhook → delayed follow‑up"** (uses Webhook, Wait, HTTP)
`Webhook trigger (POST)` → `Wait (10 minutes)` → `HTTP Request (send reminder)` → `Respond to Webhook (200 "ok")`.

**A4. "Daily report" (Sprint‑6 nodes)** (uses Schedule, HTTP + secret, Code, Sheets write)
`Schedule (daily 08:00)` → `HTTP Request (GET dashboard API, header `Authorization: Bearer {{ $secrets.API_TOKEN }}`)` → `Code (Python: total up {{ $node["HTTP Request"].body }})` → `Google Sheets append`.

---

## Appendix B — references

- React Flow (the canvas library we use): https://reactflow.dev — see the "Drag and Drop" and "Custom Nodes" examples. We already load it from `https://esm.sh/reactflow@11` in `static/flows.js`.
- n8n (the tool we're modelling): https://n8n.io — useful to copy its **node config UX** and its workflow JSON shape (nodes + connections).
- Google Sheets API: https://developers.google.com/sheets/api
- Google OAuth 2.0 for web server apps: https://developers.google.com/identity/protocols/oauth2/web-server
- Our own already‑built reference: **`pbx-monitor/automations.py`** (the engine) and **`pbx-monitor/static/flows.js`** (the React‑Flow‑from‑CDN pattern).

---

*End of sprint doc. Build it one sprint at a time, ship each sprint behind the `active` flag, and demo at the end of every sprint. Good luck — the hardest part (the engine) is already done.*
