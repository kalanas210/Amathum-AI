// Expressions {{ $json.field.path }} — safe find-and-replace, NO eval.
// Faithful port of the Python `_walk` / `_lookup` / `_render`.

import { EXPR_RE } from "./constants";
import type { Item } from "./types";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function walk(obj: unknown, dotted: string): unknown {
  let cur: unknown = obj;
  for (const part of String(dotted).split(".")) {
    if (part === "") continue; // drop empty segments (filter(None, ...))
    if (isPlainObject(cur)) {
      cur = cur[part];
    } else if (Array.isArray(cur)) {
      const t = part.trim(); // Python int() tolerates surrounding space / leading '+'
      if (!/^[+-]?\d+$/.test(t)) return null; // int(part) would raise -> None
      let idx = Number.parseInt(t, 10);
      if (idx < 0) idx += cur.length; // Python supports negative indexing
      if (idx < 0 || idx >= cur.length) return null;
      cur = cur[idx];
    } else {
      return null;
    }
  }
  return cur;
}

function lookup(rawPath: string, item: Item): unknown {
  let p = rawPath.trim();
  if (p === "$json") return item;
  if (p.startsWith("$json.")) p = p.slice("$json.".length);
  return walk(item, p);
}

/**
 * Python str() parity for scalars: booleans render as "True"/"False" (matching
 * Python's str(bool)); everything else via String(). Whole-number floats like
 * 1.0 can't be distinguished from ints after JSON.parse, so they render as "1"
 * — an inherent JS limitation vs Python's "1.0".
 */
export function pyStr(v: unknown): string {
  if (typeof v === "boolean") return v ? "True" : "False";
  return String(v);
}

/**
 * Python json.dumps() parity for objects/arrays: ", " and ": " separators.
 * (Inside JSON, booleans stay lowercase — unlike str(bool).)
 */
export function pyJson(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") {
    return Number.isFinite(v) ? String(v) : v > 0 ? "Infinity" : v < 0 ? "-Infinity" : "NaN";
  }
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(pyJson).join(", ")}]`;
  if (typeof v === "object") {
    return `{${Object.entries(v as Record<string, unknown>)
      .map(([k, val]) => `${JSON.stringify(k)}: ${pyJson(val)}`)
      .join(", ")}}`;
  }
  return "null";
}

/** Replace every {{ ... }} in a string with values from the current item. */
export function render(value: unknown, item: Item): unknown {
  if (typeof value !== "string" || !value.includes("{{")) return value;
  return value.replace(EXPR_RE, (_match, expr: string) => {
    const r = lookup(expr, item);
    if (r === null || r === undefined) return "";
    return typeof r === "object" ? pyJson(r) : pyStr(r);
  });
}
