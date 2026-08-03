// Workflow storage (one JSON file per workflow) + higher-level service helpers
// (create / update / restore / activate / import / export) so route handlers
// stay thin.

import { promises as fs } from "node:fs";
import path from "node:path";

import { isKnownNodeType } from "./catalog";
import { AUTOMATIONS_DIR, ID_RE, MAX_WF_BYTES } from "./constants";
import { atomicWrite, ensureDirs, nowIso, readJson, slugify, token } from "./io";
import { loadStats } from "./runs";
import type {
  Connections,
  Workflow,
  WorkflowListItem,
  WorkflowNode,
} from "./types";
import { loadVersion, snapshotVersion } from "./versions";

function wfPath(id: string): string {
  return path.join(AUTOMATIONS_DIR, `${id}.json`);
}

export async function loadWorkflow(id: string): Promise<Workflow | null> {
  if (!ID_RE.test(id)) return null; // guard against path traversal
  return readJson<Workflow>(wfPath(id));
}

export async function listWorkflowIds(): Promise<string[]> {
  await ensureDirs();
  const files = await fs.readdir(AUTOMATIONS_DIR);
  return files
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -".json".length))
    .sort();
}

export async function saveWorkflow(wf: Workflow): Promise<void> {
  await atomicWrite(wfPath(wf.id), JSON.stringify(wf, null, 2));
}

export async function deleteWorkflow(id: string): Promise<void> {
  if (!ID_RE.test(id)) return;
  try {
    await fs.unlink(wfPath(id));
  } catch {
    /* already gone */
  }
}

/** Return an error string if the workflow is invalid, else null. */
export function validateWorkflow(wf: unknown): string | null {
  if (typeof wf !== "object" || wf === null || Array.isArray(wf)) {
    return "workflow must be an object";
  }
  const w = wf as Record<string, unknown>;
  if (JSON.stringify(w).length > MAX_WF_BYTES) return "workflow too large";
  if (typeof w.id !== "string" || !ID_RE.test(w.id)) return "invalid id";
  if (!Array.isArray(w.nodes)) return "nodes must be a list";

  const seen = new Set<unknown>();
  for (const n of w.nodes) {
    if (typeof n !== "object" || n === null || Array.isArray(n)) {
      return "each node must be an object";
    }
    const node = n as Record<string, unknown>;
    const nid = node.id;
    if (!nid || seen.has(nid)) {
      return `duplicate or missing node id: ${JSON.stringify(nid)}`;
    }
    seen.add(nid);
    if (!isKnownNodeType(node.type)) {
      return `unknown node type: ${JSON.stringify(node.type)}`;
    }
  }
  const conns = w.connections;
  if (
    conns !== undefined &&
    (typeof conns !== "object" || conns === null || Array.isArray(conns))
  ) {
    return "connections must be an object";
  }
  return null;
}

/** Give each webhook/form trigger a stable unguessable token (+ secret). */
export function ensureTokens(wf: Workflow): void {
  for (const n of wf.nodes || []) {
    if (
      n.type === "webhookTrigger" ||
      n.type === "formTrigger" ||
      n.type === "callCompletedTrigger"
    ) {
      const p = (n.parameters ||= {}) as Record<string, unknown>;
      if (!p.path) p.path = token(16);
      if (
        (n.type === "webhookTrigger" || n.type === "callCompletedTrigger") &&
        p.auth === "header secret" &&
        !p.secret
      ) {
        p.secret = token(18);
      }
    }
  }
}

export async function findByToken(
  tok: string,
  nodeType: string | string[],
): Promise<{ wf: Workflow; node: WorkflowNode } | null> {
  const types = Array.isArray(nodeType) ? nodeType : [nodeType];
  for (const id of await listWorkflowIds()) {
    const wf = await loadWorkflow(id);
    if (!wf) continue;
    for (const n of wf.nodes || []) {
      const p = (n.parameters || {}) as Record<string, unknown>;
      if (types.includes(n.type) && p.path === tok) return { wf, node: n };
    }
  }
  return null;
}

export async function createWorkflow(name: string): Promise<Workflow> {
  const id = slugify(name, new Set(await listWorkflowIds()));
  const now = nowIso();
  const wf: Workflow = {
    id,
    name: (name || "Untitled workflow").slice(0, 80),
    description: "",
    active: false,
    version: 1,
    nodes: [
      {
        id: "trigger",
        name: "Manual / Test",
        type: "manualTrigger",
        position: { x: 80, y: 160 },
        parameters: {},
      },
    ],
    connections: {},
    created_at: now,
    updated_at: now,
  };
  await saveWorkflow(wf);
  return wf;
}

export async function listWorkflowSummaries(): Promise<WorkflowListItem[]> {
  const out: WorkflowListItem[] = [];
  for (const id of await listWorkflowIds()) {
    const wf = await loadWorkflow(id);
    if (!wf) continue;
    out.push({
      id: wf.id,
      name: wf.name,
      active: wf.active ?? false,
      version: wf.version ?? 1,
      updated_at: wf.updated_at,
      stats: await loadStats(id),
    });
  }
  return out;
}

export type WorkflowPatch = Partial<
  Pick<Workflow, "name" | "description" | "active" | "nodes" | "connections">
>;

type ServiceResult =
  | { ok: true; wf: Workflow }
  | { ok: false; notFound?: true; error?: string };

/** Update mutable fields, snapshot the previous version, bump version. */
export async function updateWorkflow(
  id: string,
  patch: WorkflowPatch,
): Promise<ServiceResult> {
  const existing = await loadWorkflow(id);
  if (!existing) return { ok: false, notFound: true };

  const wf: Workflow = { ...existing };
  if (patch.name !== undefined) wf.name = patch.name;
  if (patch.description !== undefined) wf.description = patch.description;
  if (patch.active !== undefined) wf.active = patch.active;
  if (patch.nodes !== undefined) wf.nodes = patch.nodes;
  if (patch.connections !== undefined) wf.connections = patch.connections;
  wf.id = id;

  const err = validateWorkflow(wf);
  if (err) return { ok: false, error: err };

  await snapshotVersion(existing); // keep the pre-save version
  wf.version = Math.trunc(Number(existing.version) || 1) + 1;
  wf.updated_at = nowIso();
  ensureTokens(wf); // assign webhook/form URLs
  await saveWorkflow(wf);
  return { ok: true, wf };
}

export async function restoreVersion(id: string, v: number): Promise<ServiceResult> {
  const cur = await loadWorkflow(id);
  const snap = await loadVersion(id, v);
  if (!cur || !snap) return { ok: false, notFound: true };

  await snapshotVersion(cur); // snapshot current first -> restore is undoable
  const restored: Workflow = {
    ...snap,
    id,
    version: Math.trunc(Number(cur.version) || 1) + 1,
    updated_at: nowIso(),
  };
  await saveWorkflow(restored);
  return { ok: true, wf: restored };
}

export async function setActive(
  id: string,
  active?: boolean,
): Promise<ServiceResult> {
  const wf = await loadWorkflow(id);
  if (!wf) return { ok: false, notFound: true };
  wf.active = active === undefined ? !wf.active : Boolean(active);
  wf.updated_at = nowIso();
  await saveWorkflow(wf);
  return { ok: true, wf };
}

export async function importWorkflow(
  body: Record<string, unknown>,
): Promise<ServiceResult> {
  const now = nowIso();
  const id = slugify(
    (body.name as string) || "Imported workflow",
    new Set(await listWorkflowIds()),
  );
  const wf: Workflow = {
    id,
    name: ((body.name as string) || "Imported workflow").slice(0, 80),
    description: (body.description as string) || "",
    active: false,
    version: 1,
    nodes: (body.nodes as WorkflowNode[]) || [],
    connections: (body.connections as Connections) || {},
    created_at: now,
    updated_at: now,
  };
  const err = validateWorkflow(wf);
  if (err) return { ok: false, error: err };
  ensureTokens(wf);
  await saveWorkflow(wf);
  return { ok: true, wf };
}

/** Workflow JSON for download (drops the volatile updated_at field). */
export async function exportWorkflow(
  id: string,
): Promise<Record<string, unknown> | null> {
  const wf = await loadWorkflow(id);
  if (!wf) return null;
  const clean = { ...wf } as Record<string, unknown>;
  delete clean.updated_at;
  return clean;
}
