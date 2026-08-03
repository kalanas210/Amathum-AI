// Node executors. Signature: (params, items) -> list-of-output-lists.
//   single output -> [outItems]      IF (2 outputs) -> [trueItems, falseItems]
// Faithful port of the Python executors; http uses fetch, wait uses setTimeout.

import { HTTP_MAX_BYTES, HTTP_TIMEOUT_MS, WAIT_CAP_SECONDS } from "./constants";
import { pyStr, render } from "./expressions";
import type { Executor, Item } from "./types";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function toNumber(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "string") {
    const t = v.trim();
    if (t === "") return null;
    // Python float() rejects JS-only hex/octal/binary literals (0x.., 0o.., 0b..).
    if (/^[+-]?0[xXoObB]/.test(t)) return null;
    const n = Number(t);
    return Number.isNaN(n) ? null : n;
  }
  return null;
}

function isEmptyVal(v: unknown): boolean {
  return (
    v === null ||
    v === undefined ||
    v === "" ||
    (Array.isArray(v) && v.length === 0) ||
    (isPlainObject(v) && Object.keys(v).length === 0)
  );
}

function cmp(v1: unknown, op: string, v2: unknown): boolean {
  if (op === "isEmpty") return isEmptyVal(v1);
  if (op === "isNotEmpty") return !isEmptyVal(v1);

  const s1 = v1 === null || v1 === undefined ? "" : pyStr(v1);
  const s2 = v2 === null || v2 === undefined ? "" : pyStr(v2);
  if (op === "equal") return s1 === s2;
  if (op === "notEqual") return s1 !== s2;
  if (op === "contains") return s1.includes(s2);
  if (op === "notContains") return !s1.includes(s2);
  if (op === "regex") {
    try {
      return new RegExp(s2).test(s1);
    } catch {
      return false;
    }
  }

  const n1 = toNumber(v1);
  const n2 = toNumber(v2);
  if (n1 === null || n2 === null) return false;
  if (op === "gt") return n1 > n2;
  if (op === "lt") return n1 < n2;
  if (op === "gte") return n1 >= n2;
  if (op === "lte") return n1 <= n2;
  return false;
}

function clampWaitSeconds(params: Record<string, unknown>): number {
  const amt = toNumber(params.amount ?? 0) ?? 0;
  const unit = (params.unit as string) || "seconds";
  const mult = unit === "minutes" ? 60 : unit === "hours" ? 3600 : 1;
  return Math.max(0, Math.min(WAIT_CAP_SECONDS, amt * mult));
}

function hasHeader(headers: Record<string, string>, name: string): boolean {
  const lower = name.toLowerCase();
  return Object.keys(headers).some((k) => k.toLowerCase() === lower);
}

// ---------------------------------------------------------------------------

const execManual: Executor = (_params, items) => [
  items && items.length ? [...items] : [{}],
];

const execSet: Executor = (params, items) => {
  const keep = Boolean(params.keepOnlySet);
  const assigns = (params.assignments as Array<Record<string, unknown>>) || [];
  const out: Item[] = [];
  for (const it of items) {
    const base: Item = keep ? {} : { ...it };
    for (const a of assigns) {
      const name = a?.name as string | undefined;
      // Python uses a.get("value", "") — default only when the key is absent;
      // an explicit null stays null (render passes non-strings through).
      if (name) base[name] = render("value" in a ? a.value : "", it);
    }
    out.push(base);
  }
  return [out];
};

const execIf: Executor = (params, items) => {
  const op = (params.operator as string) || "equal";
  const t: Item[] = [];
  const f: Item[] = [];
  for (const it of items) {
    const v1 = render(params.value1 ?? "", it);
    const v2 = render(params.value2 ?? "", it);
    (cmp(v1, op, v2) ? t : f).push(it);
  }
  return [t, f];
};

const execWait: Executor = async (params, items) => {
  const secs = clampWaitSeconds(params);
  if (secs > 0) await new Promise((r) => setTimeout(r, secs * 1000));
  return [[...items]];
};

const execHttp: Executor = async (params, items) => {
  const method = String(params.method || "GET").toUpperCase();
  const bodyType = (params.bodyType as string) ?? "none";
  const rawHeaders = (params.headers as Record<string, unknown>) || {};
  const out: Item[] = [];

  for (const it of items) {
    const result: Item = { ...it };
    const url = String(render(params.url ?? "", it) ?? "");
    if (!url) {
      result._http = { error: "no url" };
      out.push(result);
      continue;
    }

    const headers: Record<string, string> = {};
    if (isPlainObject(rawHeaders)) {
      for (const [k, v] of Object.entries(rawHeaders)) {
        headers[k] = typeof v === "string" ? String(render(v, it)) : String(v);
      }
    }

    let data: string | undefined;
    if (bodyType === "json" || bodyType === "raw") {
      const body = render(params.body ?? "", it);
      data = typeof body === "string" ? body : JSON.stringify(body);
      if (bodyType === "json" && !hasHeader(headers, "content-type")) {
        headers["Content-Type"] = "application/json";
      }
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HTTP_TIMEOUT_MS);
    try {
      const sendsBody = method !== "GET" && method !== "HEAD";
      const resp = await fetch(url, {
        method,
        headers,
        body: sendsBody ? data : undefined,
        signal: controller.signal,
        redirect: "follow",
      });
      const ct = resp.headers.get("content-type") || "";
      const raw = await resp.text();
      const text = raw.length > HTTP_MAX_BYTES ? raw.slice(0, HTTP_MAX_BYTES) : raw;
      let parsed: unknown = text;
      if (ct.toLowerCase().includes("json")) {
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = text;
        }
      }
      const respHeaders: Record<string, string> = {};
      resp.headers.forEach((value, key) => {
        respHeaders[key] = value;
      });
      result._http = { statusCode: resp.status, headers: respHeaders, body: parsed };
    } catch (e) {
      // network failure / timeout -> soft error
      const msg =
        e instanceof Error
          ? e.name === "AbortError"
            ? `timeout after ${HTTP_TIMEOUT_MS}ms`
            : e.message
          : String(e);
      result._http = { error: msg };
    } finally {
      clearTimeout(timer);
    }
    out.push(result);
  }
  return [out];
};

// Pass items through unchanged; the engine captures the HTTP response separately.
const execRespond: Executor = (_params, items) => [[...items]];

// Stub for nodes whose real engine ships in a later sprint (Sheets / Code /
// OpenAI). Forwards input unchanged so flows stay runnable end-to-end.
const execPassthrough: Executor = (_params, items) => [
  items && items.length ? [...items] : [{}],
];

export const EXECUTORS: Record<string, Executor> = {
  manualTrigger: execManual,
  webhookTrigger: execManual, // triggers just pass their payload through
  formTrigger: execManual,
  scheduleTrigger: execManual,
  callCompletedTrigger: execManual, // AI call-completed trigger (payload passthrough)
  httpRequest: execHttp,
  wait: execWait,
  if: execIf,
  set: execSet,
  respondToWebhook: execRespond,
  googleSheets: execPassthrough, // visual stub (real I/O = later sprint)
  code: execPassthrough, // visual stub (sandboxed run = later sprint)
  openAi: execPassthrough, // visual stub (model call = later sprint)
};
