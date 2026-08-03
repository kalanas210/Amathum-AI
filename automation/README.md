# Ryzera Automations — `automation/`

An **n8n-style workflow builder** ("if-this-then-then") for the Ryzera / Amathum AI admin.
This folder is the **Sprint-1 backend foundation** from `automations-sprint-build-guide.md`,
built as a **self-contained, independent project** so it can be tested on its own.

> ⚠️ This folder does **not** import or modify anything in `voip-recovery-staging/`,
> `sampath/`, etc. It runs standalone today; we wire it into the main `pbx-monitor`
> Flask app later (one function call — see *Integration* below).

## What's here

| File | Purpose |
|---|---|
| `automations.py` | The engine: data model, `run_workflow`, the 5 core node executors (manual / http / wait / if / set), `{{ }}` expressions, JSON storage, and the HTTP API (`init_automations`). Pure stdlib except Flask (for the API only). |
| `run.py` | A standalone dev server (no auth) so you can click/curl the feature in isolation. |
| `test_automations.py` | Unit + API tests (uses a throwaway temp data dir). |
| `requirements.txt` | `Flask` (the engine itself needs no third-party packages). |
| `data/` | Local storage created at runtime (workflows, run logs, stats). Git-ignored. |

## Run it

```bash
cd automation
pip install -r requirements.txt          # only Flask
python run.py                            # http://127.0.0.1:5099
```

**Open the visual builder:** http://127.0.0.1:5099/automations — drag nodes from the
left palette onto the canvas, wire them up, configure each on the right, then **Save** /
**Run** (the run-log shows per-node results). Version **History**, **Export** and **Import**
are in the top bar. (React Flow loads from CDN; needs internet on first load.)

Then try the API directly:

```bash
# list node types
curl localhost:5099/api/automations/_node-catalog

# create a workflow
WID=$(curl -s -X POST localhost:5099/api/automations \
        -H 'Content-Type: application/json' -d '{"name":"My Flow"}' \
        | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# run it (manual trigger)
curl -s -X POST localhost:5099/api/automations/$WID/run \
        -H 'Content-Type: application/json' -d '{}'
```

## Test it

```bash
cd automation
python test_automations.py               # or: python -m unittest test_automations -v
```

Covers: expressions, IF routing (string/number/regex/empty), branch pruning, Wait
cap, Set (incl. keepOnlySet), HTTP soft-errors, save/load + validation, full
`run_workflow`, and the JSON API end-to-end (create → save → run → runs/stats).

## What it does (the model)

- A **workflow** is one JSON file (`data/automations/<id>.json`): `nodes` + `connections`.
- **Data between nodes is a list of items** (JSON objects); most nodes run once per item.
- **`run_workflow`** finds the trigger, walks the graph in topological order, runs each
  node's executor, and routes outputs along the connections. A node only fires if it
  received items (so an IF's unused branch does nothing). Every run is logged with a
  per-node status + a run-level status (`success` / `failed`).
- **Expressions** `{{ $json.field.path }}` are a safe find-and-replace — **no `eval`**.

### Nodes implemented (Sprint 1)
`manualTrigger` · `httpRequest` · `wait` · `if` · `set`
(The catalog also lists later-sprint nodes as `implemented: false` so the future
canvas can show what's coming: webhook/form/sheets/schedule/code/respond.)

## API (all under `/api/automations`)

| Method & path | Purpose | Sprint |
|---|---|---|
| `GET /automations` | the visual builder page (HTML) | 2 |
| `GET /_node-catalog` | node types + their config fields (drives the palette/panel) | 1 |
| `GET /` | list workflows + run stats | 1 |
| `POST /` | create (from a name) | 1 |
| `GET /<id>` · `PUT /<id>` · `DELETE /<id>` | load / save (validates, snapshots) / delete | 1 |
| `POST /<id>/run` | run now (manual); returns the run log | 1 |
| `GET /<id>/runs` | recent run logs | 1 |
| `GET /<id>/versions` · `GET /<id>/versions/<v>` | list / load saved versions | 2 |
| `POST /<id>/restore/<v>` | restore a version (snapshots current first) | 2 |
| `GET /<id>/export` · `POST /import` | download / upload a workflow JSON | 2 |

## Integration (later — not done here)

The engine is auth-agnostic. To mount it inside the real `pbx-monitor` `app.py`,
add two lines before `app.run(...)` and pass the host app's decorators so the routes
inherit the existing admin login:

```python
from automations import init_automations
init_automations(app, login_required=login_required, perm_required=perm_required)
```

Standalone (`run.py`) calls `init_automations(app)` with no decorators, so the dev
server is open for local testing only.

## Sprints

- **Sprint 1 ✅** — engine, data model, JSON API (the 5 core nodes).
- **Sprint 2 ✅** — visual canvas (`templates/automations.html` + `static/automations.js`, React Flow from CDN), config panels, Save/Run + run-log, run stats, version history + restore, export/import.
- **Sprint 3 (next)** — real triggers: webhook + form (the *public* routes) + Respond-to-Webhook + the `active` toggle.
- **Sprint 4–6** — Google Sheets, durable Wait, schedule/cron, code node, secrets.
