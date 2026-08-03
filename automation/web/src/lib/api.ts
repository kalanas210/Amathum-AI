// Typed browser client for the automations API. Client components call these;
// server components call the engine in `@/lib/engine` directly.

import type {
  NodeCatalogEntry,
  RunLog,
  VersionMeta,
  Workflow,
  WorkflowListItem,
} from "@/lib/engine/types";

const BASE = "/api/automations";

export type WorkflowPatch = Partial<
  Pick<Workflow, "name" | "description" | "active" | "nodes" | "connections">
>;

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  if (!res.ok) {
    const msg =
      (data as { error?: string } | null)?.error || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data as T;
}

function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  return req<T>(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export const api = {
  catalog: () =>
    req<{ nodes: NodeCatalogEntry[] }>(`${BASE}/node-catalog`).then((d) => d.nodes),

  list: () => req<{ workflows: WorkflowListItem[] }>(BASE).then((d) => d.workflows),
  create: (name: string) => send<Workflow>(BASE, "POST", { name }),
  get: (id: string) => req<Workflow>(`${BASE}/${id}`),
  save: (id: string, patch: WorkflowPatch) =>
    send<Workflow>(`${BASE}/${id}`, "PUT", patch),
  remove: (id: string) => send<{ ok: boolean }>(`${BASE}/${id}`, "DELETE"),

  run: (id: string, payload?: unknown) =>
    send<RunLog>(`${BASE}/${id}/run`, "POST", { payload }),
  runs: (id: string) =>
    req<{ runs: RunLog[] }>(`${BASE}/${id}/runs`).then((d) => d.runs),

  versions: (id: string) =>
    req<{ versions: VersionMeta[] }>(`${BASE}/${id}/versions`).then(
      (d) => d.versions,
    ),
  version: (id: string, v: number) => req<Workflow>(`${BASE}/${id}/versions/${v}`),
  restore: (id: string, v: number) =>
    send<Workflow>(`${BASE}/${id}/restore/${v}`, "POST"),

  setActive: (id: string, active: boolean) =>
    send<Workflow>(`${BASE}/${id}/activate`, "POST", { active }),

  import: (body: unknown) => send<Workflow>(`${BASE}/import`, "POST", body),
  exportUrl: (id: string) => `${BASE}/${id}/export`,
};
