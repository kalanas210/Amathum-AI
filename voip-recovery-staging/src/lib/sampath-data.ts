/**
 * Live data cache for Sampath Bank queries.
 *
 * Pulls from sampath.lk's own public JSON API on startup and every hour,
 * keeps everything in memory so tool calls return in <1 ms — no internet
 * round-trip during the live conversation.
 *
 * Endpoints (verified May 2026):
 *   /api/branches        313 branches (name / address / phone / manager / email / GPS / province)
 *   /api/exchange-rates  17 currencies (TT buy/sell, OD buy)
 *   /api/contact-us      head-office + departmental contacts
 */

const SAMPATH_BASE = "https://www.sampath.lk";
const REFRESH_INTERVAL_MS = 60 * 60 * 1000; // 1 hour

const UA = "Mozilla/5.0 (compatible; SampathAI/1.0; +https://monitor.easmoney.me)";
const FETCH_TIMEOUT_MS = 10000;

export interface SampathBranch {
  id: string;
  branch_name: string;
  address: string;
  contact_no: string;
  email: string;
  province: string;
  manager_name: string;
  category: string;          // BRANCH / SAMPATH MOBILE BANK / etc.
  latitude: string;
  longitude: string;
  fax?: string;
  opening_hours?: string;
}

export interface SampathRate {
  CurrCode: string;          // e.g. "USD"
  CurrName: string;          // e.g. "U.S. Dollar"
  TTBUY: string;             // bank buys at
  TTSEL: string;             // bank sells at
  ODBUY: string;             // OD buy rate
  RateWEF: string;           // timestamp the rate is effective from
}

interface Cache {
  branches: SampathBranch[];
  rates: SampathRate[];
  ratesEffectiveFrom: string;
  lastRefresh: number;       // epoch ms
  lastError: string | null;
}

const cache: Cache = {
  branches: [],
  rates: [],
  ratesEffectiveFrom: "",
  lastRefresh: 0,
  lastError: null,
};

async function fetchJson(path: string): Promise<unknown> {
  const ctrl = new AbortController();
  const tm = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(`${SAMPATH_BASE}${path}`, {
      headers: { "User-Agent": UA, Accept: "application/json" },
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  } finally {
    clearTimeout(tm);
  }
}

export async function refreshSampathData(): Promise<void> {
  try {
    const [branches, ratesResp] = await Promise.all([
      fetchJson("/api/branches"),
      fetchJson("/api/exchange-rates"),
    ]);
    if (Array.isArray(branches)) {
      cache.branches = branches as SampathBranch[];
    }
    if (
      ratesResp &&
      typeof ratesResp === "object" &&
      Array.isArray((ratesResp as any).data)
    ) {
      cache.rates = (ratesResp as any).data as SampathRate[];
      cache.ratesEffectiveFrom = cache.rates[0]?.RateWEF || "";
    }
    cache.lastRefresh = Date.now();
    cache.lastError = null;
    console.log(
      `[sampath-data] refresh ok — ${cache.branches.length} branches, ${cache.rates.length} rates (effective ${cache.ratesEffectiveFrom})`
    );
  } catch (e) {
    cache.lastError = (e as Error).message;
    console.error(`[sampath-data] refresh failed: ${cache.lastError}`);
  }
}

export function startBackgroundRefresh(): void {
  // Initial fetch is awaited by caller; this just schedules subsequent ones.
  setInterval(() => {
    refreshSampathData().catch((e) =>
      console.error("[sampath-data] background refresh:", e)
    );
  }, REFRESH_INTERVAL_MS);
}

export function getCacheStats(): {
  branches: number;
  rates: number;
  age_minutes: number;
  last_error: string | null;
} {
  return {
    branches: cache.branches.length,
    rates: cache.rates.length,
    age_minutes: Math.round((Date.now() - cache.lastRefresh) / 60000),
    last_error: cache.lastError,
  };
}

// ---------- Search ----------

const NORMALISE_RE = /[^a-z0-9඀-෿஀-௿]+/gi;
function norm(s: string): string {
  return (s || "").toLowerCase().replace(NORMALISE_RE, "");
}

export function findBranches(rawQuery: string, limit = 4): SampathBranch[] {
  if (!rawQuery) return [];
  const q = norm(rawQuery);
  if (!q) return [];
  // Score each branch:
  //   exact name match     → 10
  //   name starts-with     → 6
  //   name contains        → 4
  //   address contains     → 2
  const scored: { b: SampathBranch; score: number }[] = [];
  for (const b of cache.branches) {
    const n = norm(b.branch_name);
    const a = norm(b.address);
    let score = 0;
    if (n === q) score = 10;
    else if (n.startsWith(q)) score = 6;
    else if (n.includes(q)) score = 4;
    else if (a.includes(q)) score = 2;
    if (score > 0) scored.push({ b, score });
  }
  scored.sort((x, y) => y.score - x.score);
  return scored.slice(0, limit).map((s) => s.b);
}

export function getRates(currencyHint?: string): SampathRate[] {
  if (!currencyHint) return cache.rates;
  const q = norm(currencyHint);
  if (!q) return cache.rates;
  const matched = cache.rates.filter((r) => {
    return (
      norm(r.CurrCode) === q ||
      norm(r.CurrCode).includes(q) ||
      norm(r.CurrName).includes(q)
    );
  });
  return matched.length ? matched : cache.rates;
}

// ---------- Tool-result formatters (clean output for Gemini) ----------

export function formatBranchForAgent(b: SampathBranch): Record<string, string> {
  return {
    name: b.branch_name,
    address: b.address,
    phone: b.contact_no || "",
    email: b.email || "",
    manager: b.manager_name || "",
    province: b.province || "",
    category: b.category || "BRANCH",
    fax: b.fax || "",
  };
}

export function formatRateForAgent(r: SampathRate): Record<string, string> {
  return {
    code: r.CurrCode,
    name: r.CurrName,
    bank_buys_at_lkr: r.TTBUY,        // we buy your foreign currency at this rate
    bank_sells_at_lkr: r.TTSEL,       // we sell foreign currency to you at this rate
    od_buy_rate: r.ODBUY,
    effective_from: r.RateWEF,
  };
}
