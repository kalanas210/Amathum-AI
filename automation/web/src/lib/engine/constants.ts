import path from "node:path";

// Storage config — env-overridable; local defaults so it runs without root.
// Defaults to `<cwd>/data` (i.e. web/data during `next dev`).
const DATA_BASE =
  process.env.AUTOMATIONS_DATA_DIR || path.join(process.cwd(), "data");

export const DATA_DIR = DATA_BASE;
export const AUTOMATIONS_DIR =
  process.env.AUTOMATIONS_DIR || path.join(DATA_BASE, "automations");
export const RUNS_DIR =
  process.env.AUTOMATIONS_RUNS_DIR || path.join(DATA_BASE, "automations-runs");
export const STATE_DIR =
  process.env.AUTOMATIONS_STATE_DIR || path.join(DATA_BASE, "automations-state");
export const VERSIONS_DIR =
  process.env.AUTOMATIONS_VERSIONS_DIR ||
  path.join(DATA_BASE, "automations-versions");

export const MAX_WF_BYTES = 256 * 1024; // reject oversized workflows
export const KEEP_RUNS = 25; // run-log retention per workflow
export const KEEP_VERSIONS = 30; // version-history retention per workflow
export const WAIT_CAP_SECONDS = 15; // inline Wait cap until durable wait
export const HTTP_TIMEOUT_MS = 20_000; // HTTP node timeout
export const HTTP_MAX_BYTES = 1024 * 1024; // HTTP node response cap

export const ID_RE = /^[a-z0-9][a-z0-9-]{1,62}$/;
export const EXPR_RE = /\{\{(.*?)\}\}/g;

export const TRIGGER_TYPES = new Set([
  "manualTrigger",
  "webhookTrigger",
  "formTrigger",
  "scheduleTrigger",
  "callCompletedTrigger",
]);
