# Ryzera Automations — Next.js app (`web/`)

An **n8n-style workflow builder** for the Ryzera / Amathum AI admin, rewritten from the
original Flask + vanilla-JS prototype into a single **Next.js (App Router)** app with a
**TypeScript** engine, **Next.js Route Handlers** for the API, and a **ShadCN + React
Flow** UI.

## Stack

| Layer        | Choice                                                          |
|--------------|----------------------------------------------------------------|
| Framework    | Next.js 16 (App Router, Turbopack), React 19                   |
| Language     | TypeScript                                                      |
| UI           | ShadCN (Base UI) + Tailwind CSS v4, `@xyflow/react` (React Flow)|
| Theme        | `next-themes` (light/dark), Sonner toasts, Lucide icons        |
| Engine       | Pure-TS port of the Python engine (no framework deps)          |
| Storage      | JSON files on disk (env-overridable), same schema as before    |
| Tests        | Vitest (engine parity suite)                                   |

## Run it

```bash
cd web
npm install
npm run dev          # http://localhost:3000  (redirects to /automations)
```

- **Dashboard:** `/automations` — list / create / import / run / activate / delete.
- **Builder:** `/automations/<id>` — drag nodes, wire them, configure, Save / Run, run
  log, version history, export.
- **Public form:** `/form/<token>` — hosted form for `formTrigger` nodes.
- **Webhook:** `POST /api/automations/hook/<token>` — public webhook trigger.

```bash
npm run build        # production build
npx vitest run       # engine parity tests
```

## Folder structure

```
src/
  app/
    layout.tsx                     # root layout: providers + Sonner toaster
    page.tsx                       # redirect -> /automations
    globals.css                    # Tailwind v4 theme tokens + n8n node-group colors
    automations/
      page.tsx                     # dashboard (server component -> engine)
      [id]/page.tsx                # builder (server component -> <Builder>)
    form/[token]/page.tsx          # public hosted form (server action submit)
    api/automations/               # Route Handlers (the API)
      route.ts                     # GET list, POST create
      node-catalog/route.ts        # GET catalog
      import/route.ts              # POST import
      [id]/route.ts                # GET / PUT / DELETE
      [id]/run, runs, versions, versions/[v], restore/[v], export, activate
      hook/[token]/route.ts        # PUBLIC webhook
  lib/
    engine/                        # the workflow engine (pure TS, no Next deps)
      types · constants · catalog · expressions · executors · run
      io · workflows · runs · versions · index · engine.test.ts
    api.ts                         # typed browser client for the API
    node-meta.ts                   # icon + group-color metadata
    format.ts · http.ts · utils.ts
  components/
    providers.tsx · ui/            # ShadCN primitives
    automations/
      workflow-dashboard · status-badge · time-ago · theme-toggle
      builder/
        builder.tsx                # the builder "brain" (state + handlers)
        builder-context · flow.ts  # graph <-> workflow conversion
        flow-canvas · node-card    # React Flow canvas + custom node
        node-picker · config-panel · run-log · version-history · top-bar
        trigger-info · param-fields/ (field-list, form-fields, dispatcher)
```

## Architecture notes

- **Server components call the engine directly** (`@/lib/engine`); **client components
  call the API** (`@/lib/api`). This keeps Node-only code (`fs`) out of the client bundle —
  client code only imports `@/lib/engine/types` (types are erased at compile time).
- **`params` is async** in Next 16 (route handlers and pages `await params`).
- The engine is a faithful port of the original `automations.py` (same data model and
  semantics), so existing `data/` workflow files load unchanged.

## Storage

Data lives in `web/data/` by default (git-ignored). Override with env vars:
`AUTOMATIONS_DATA_DIR`, `AUTOMATIONS_DIR`, `AUTOMATIONS_RUNS_DIR`,
`AUTOMATIONS_STATE_DIR`, `AUTOMATIONS_VERSIONS_DIR`.

## Status / parity with the Flask prototype

Sprint-1 + Sprint-2 features are fully ported: the 5 core nodes (manual / http / wait /
if / set) plus the promoted top nodes (webhook / form / respond / schedule / sheets /
code / openai — the last few are safe pass-through stubs, as in the original), the visual
canvas, config panels, Save / Run + run log, run stats, version history + restore,
export / import, the public webhook, and the hosted form.
