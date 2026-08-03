// Run logs + per-workflow stats, with retention.

import { promises as fs } from "node:fs";
import path from "node:path";

import { ID_RE, KEEP_RUNS, RUNS_DIR, STATE_DIR } from "./constants";
import { atomicWrite, nowIso, readJson, uid12 } from "./io";
import type { RunLog, WorkflowStats } from "./types";

const DEFAULT_STATS: WorkflowStats = {
  total: 0,
  succeeded: 0,
  failed: 0,
  last_run_at: null,
  last_status: null,
};

async function sortedJsonByMtime(
  dir: string,
): Promise<{ file: string; mtime: number }[]> {
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return [];
  }
  const stats = await Promise.all(
    names
      .filter((n) => n.endsWith(".json"))
      .map(async (n) => {
        const file = path.join(dir, n);
        try {
          const st = await fs.stat(file);
          return { file, mtime: st.mtimeMs };
        } catch {
          return { file, mtime: 0 };
        }
      }),
  );
  return stats.sort((a, b) => b.mtime - a.mtime);
}

async function pruneDir(dir: string, keep: number): Promise<void> {
  const entries = await sortedJsonByMtime(dir);
  for (const { file } of entries.slice(keep)) {
    try {
      await fs.unlink(file);
    } catch {
      /* ignore */
    }
  }
}

export async function loadStats(wfId: string): Promise<WorkflowStats> {
  if (!ID_RE.test(wfId)) return { ...DEFAULT_STATS };
  const s = await readJson<Partial<WorkflowStats>>(
    path.join(STATE_DIR, wfId, "stats.json"),
  );
  return { ...DEFAULT_STATS, ...(s || {}) };
}

async function updateStats(wfId: string, run: RunLog): Promise<void> {
  const dir = path.join(STATE_DIR, wfId);
  await fs.mkdir(dir, { recursive: true });
  const stats = await loadStats(wfId);
  stats.total += 1;
  if (run.status === "failed") stats.failed += 1;
  else stats.succeeded += 1;
  stats.last_run_at = run.finished_at || nowIso();
  stats.last_status = run.status;
  await atomicWrite(path.join(dir, "stats.json"), JSON.stringify(stats, null, 2));
}

export async function saveRun(wfId: string, run: RunLog): Promise<string> {
  if (!ID_RE.test(wfId)) return ""; // never write outside the data dir
  const dir = path.join(RUNS_DIR, wfId);
  await fs.mkdir(dir, { recursive: true });
  run.id = run.id || uid12();
  await atomicWrite(path.join(dir, `${run.id}.json`), JSON.stringify(run, null, 2));
  await pruneDir(dir, KEEP_RUNS);
  await updateStats(wfId, run);
  return run.id;
}

export async function listRuns(wfId: string, limit = KEEP_RUNS): Promise<RunLog[]> {
  if (!ID_RE.test(wfId)) return []; // guard against path traversal via :id
  const entries = await sortedJsonByMtime(path.join(RUNS_DIR, wfId));
  const out: RunLog[] = [];
  for (const { file } of entries.slice(0, limit)) {
    const r = await readJson<RunLog>(file);
    if (r) out.push(r);
  }
  return out;
}
