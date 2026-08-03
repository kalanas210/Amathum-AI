import crypto from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";

import {
  AUTOMATIONS_DIR,
  ID_RE,
  RUNS_DIR,
  STATE_DIR,
  VERSIONS_DIR,
} from "./constants";

/** UTC ISO timestamp without milliseconds, e.g. 2026-06-14T12:34:56Z. */
export function nowIso(): string {
  return new Date().toISOString().replace(/\.\d+Z$/, "Z");
}

export async function ensureDirs(): Promise<void> {
  await Promise.all(
    [AUTOMATIONS_DIR, RUNS_DIR, STATE_DIR, VERSIONS_DIR].map((d) =>
      fs.mkdir(d, { recursive: true }),
    ),
  );
}

/** Write atomically (.tmp -> rename) so a crash never leaves a half file. */
export async function atomicWrite(filePath: string, text: string): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.tmp`;
  await fs.writeFile(tmp, text, "utf-8");
  await fs.rename(tmp, filePath);
}

export async function readJson<T>(filePath: string): Promise<T | null> {
  try {
    return JSON.parse(await fs.readFile(filePath, "utf-8")) as T;
  } catch {
    return null;
  }
}

/** 12 hex chars, like Python's uuid4().hex[:12]. */
export function uid12(): string {
  return crypto.randomBytes(6).toString("hex");
}

/** URL-safe token, like Python's secrets.token_urlsafe(nbytes). */
export function token(nbytes = 16): string {
  return crypto.randomBytes(nbytes).toString("base64url");
}

/** Generate a valid, unique id from a name (matches the Python field rules). */
export function slugify(name: string, existing: Set<string>): string {
  let base = (name || "")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  base = base.replace(/-{2,}/g, "-").slice(0, 63).replace(/-+$/g, "");
  if (!base) base = "workflow";
  if (!ID_RE.test(base)) base = `wf-${base}`.slice(0, 63).replace(/-+$/g, "");

  let cand = base;
  let i = 2;
  while (existing.has(cand) || !ID_RE.test(cand)) {
    cand = `${base.slice(0, 60)}-${i}`;
    i += 1;
  }
  return cand;
}
