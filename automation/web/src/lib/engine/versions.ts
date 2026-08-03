// Version history — snapshot on every save; restore is non-destructive.

import { promises as fs } from "node:fs";
import path from "node:path";

import { ID_RE, KEEP_VERSIONS, VERSIONS_DIR } from "./constants";
import { atomicWrite, readJson } from "./io";
import type { VersionMeta, Workflow } from "./types";

async function listSnapNumbers(dir: string): Promise<number[]> {
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return [];
  }
  return names
    .filter((n) => /^\d+\.json$/.test(n))
    .map((n) => Number.parseInt(n, 10))
    .sort((a, b) => b - a);
}

export async function snapshotVersion(wf: Workflow | null): Promise<void> {
  if (!wf || !ID_RE.test(wf.id)) return;
  const dir = path.join(VERSIONS_DIR, wf.id);
  await fs.mkdir(dir, { recursive: true });
  const v = Math.trunc(Number(wf.version) || 1);
  await atomicWrite(path.join(dir, `${v}.json`), JSON.stringify(wf, null, 2));
  const snaps = await listSnapNumbers(dir);
  for (const n of snaps.slice(KEEP_VERSIONS)) {
    try {
      await fs.unlink(path.join(dir, `${n}.json`));
    } catch {
      /* ignore */
    }
  }
}

export async function listVersions(wfId: string): Promise<VersionMeta[]> {
  if (!ID_RE.test(wfId)) return []; // guard against path traversal via :id
  const dir = path.join(VERSIONS_DIR, wfId);
  const out: VersionMeta[] = [];
  for (const n of await listSnapNumbers(dir)) {
    const wf = await readJson<Workflow>(path.join(dir, `${n}.json`));
    if (wf) {
      out.push({
        version: wf.version,
        saved_at: wf.updated_at,
        name: wf.name,
        nodes: (wf.nodes || []).length,
      });
    }
  }
  return out;
}

export async function loadVersion(wfId: string, v: number): Promise<Workflow | null> {
  if (!ID_RE.test(wfId)) return null; // guard against path traversal via :id
  return readJson<Workflow>(path.join(VERSIONS_DIR, wfId, `${Math.trunc(v)}.json`));
}
