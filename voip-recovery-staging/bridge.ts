/**
 * AudioSocket <-> Gemini Live bridge — V2 (tool calling, escalation, session log)
 *
 * Two TCP listeners:
 *   - 9090 = primary entry. Agent says configured greeting.
 *   - 9091 = retry entry (after failed manager transfer). Agent says retry greeting.
 *
 * Per call we:
 *   - parse the AudioSocket frame protocol
 *   - look up the Asterisk channel for this UUID (via /var/lib/sampath-ai/channels/<uuid>)
 *   - open a Gemini Live session pre-loaded with system prompt + tools
 *   - pipe PCM both ways with low-pass + resampling (Asterisk slin8 <-> Gemini 16kHz in / 24kHz out)
 *   - persist customer info (save_customer_info), escalate to manager (request_human_transfer),
 *     or hang up (end_call) when the model calls those tools
 *   - append every event to /var/lib/sampath-ai/sessions/<uuid>.jsonl for the admin UI
 *
 * Protocol:
 *   Each frame = 1 byte type | 2 byte BE length | payload
 *   0x00 Hangup | 0x01 UUID (16 bytes BE first) | 0x10 Audio (slin) | 0xFF Error
 */

import * as net from "net";
import * as fs from "fs";
import * as path from "path";
import * as dotenv from "dotenv";
import { monitorEventLoopDelay } from "perf_hooks";
import { createGeminiLiveSession, GeminiLiveSession, ToolCallEvent } from "./src/lib/gemini-live";
import { loadAgentConfig, AgentConfig, WorkingHoursConfig } from "./src/lib/agent-config";
import {
  refreshSampathData,
  startBackgroundRefresh,
  findBranches,
  getRates,
  formatBranchForAgent,
  formatRateForAgent,
  getCacheStats,
} from "./src/lib/sampath-data";

dotenv.config();

const LISTEN_HOST = process.env.AUDIOSOCKET_HOST || "127.0.0.1";
const LISTEN_PORT_MAIN = parseInt(process.env.AUDIOSOCKET_PORT || "9090", 10);
const LISTEN_PORT_RETRY = parseInt(process.env.AUDIOSOCKET_PORT_RETRY || "9091", 10);
const LISTEN_PORT_OUTBOUND = parseInt(process.env.AUDIOSOCKET_PORT_OUTBOUND || "9092", 10);

const SESSIONS_DIR = process.env.SAMPATH_SESSIONS_DIR || "/var/lib/sampath-ai/sessions";
const CUSTOMERS_DIR = process.env.SAMPATH_CUSTOMERS_DIR || "/var/lib/sampath-ai/customers";
const CHANNEL_REGISTRY_DIR =
  process.env.SAMPATH_CHANNEL_DIR || "/var/lib/sampath-ai/channels";
const REFDATA_DIR = process.env.SAMPATH_REFDATA_DIR || "/var/lib/sampath-ai/refdata";
const BOOKINGS_DIR = process.env.SAMPATH_BOOKINGS_DIR || "/var/lib/sampath-ai/bookings";
const OUTBOUND_DIR = process.env.SAMPATH_OUTBOUND_DIR || "/var/lib/sampath-ai/outbound";  // per-call order context for outbound confirm calls

for (const d of [SESSIONS_DIR, CUSTOMERS_DIR, CHANNEL_REGISTRY_DIR, REFDATA_DIR, OUTBOUND_DIR, path.join(BOOKINGS_DIR, "hospital", "appointments"), path.join(BOOKINGS_DIR, "reservations", "reservations")]) {
  try {
    fs.mkdirSync(d, { recursive: true });
  } catch (e) {
    console.warn(`[bridge] could not mkdir ${d}:`, (e as Error).message);
  }
}

// ============================================================
// AMI client (minimal — Originate / Redirect / Hangup / Login)
// ============================================================

interface AmiCreds {
  user: string;
  secret: string;
}

function loadAmiCreds(): AmiCreds {
  const path1 = "/opt/pbx-monitor/instance/ami.json";
  try {
    const raw = fs.readFileSync(path1, "utf-8");
    const j = JSON.parse(raw);
    return { user: j.user, secret: j.secret };
  } catch (e) {
    console.error("[ami] could not load creds:", (e as Error).message);
    return { user: "", secret: "" };
  }
}

const AMI_CREDS = loadAmiCreds();

function amiAction(
  action: Record<string, string>
): Promise<Record<string, string>> {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    let buf = "";
    let stage: "banner" | "login" | "action" | "done" = "banner";
    let result: Record<string, string> = {};

    const finish = (err: Error | null, res?: Record<string, string>) => {
      try {
        sock.end();
      } catch (_) {}
      if (err) reject(err);
      else resolve(res || {});
    };

    sock.setTimeout(8000);
    sock.on("timeout", () => finish(new Error("AMI timeout")));
    sock.on("error", (e) => finish(e));

    sock.connect(5038, "127.0.0.1", () => {
      // wait for banner before sending login
    });

    sock.on("data", (chunk) => {
      buf += chunk.toString();

      if (stage === "banner" && buf.includes("Asterisk Call Manager")) {
        stage = "login";
        sock.write(
          `Action: Login\r\nUsername: ${AMI_CREDS.user}\r\nSecret: ${AMI_CREDS.secret}\r\n\r\n`
        );
        buf = ""; // discard banner
        return;
      }

      // process complete \r\n\r\n messages
      while (buf.includes("\r\n\r\n")) {
        const idx = buf.indexOf("\r\n\r\n");
        const msg = buf.slice(0, idx);
        buf = buf.slice(idx + 4);

        const parsed: Record<string, string> = {};
        for (const line of msg.split("\r\n")) {
          const colon = line.indexOf(":");
          if (colon > 0) {
            parsed[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
          }
        }

        if (stage === "login") {
          if (parsed.Response === "Success") {
            stage = "action";
            const lines = Object.entries(action)
              .map(([k, v]) => `${k}: ${v}`)
              .join("\r\n");
            sock.write(lines + "\r\n\r\n");
          } else if (parsed.Response === "Error") {
            finish(new Error("AMI login failed: " + parsed.Message));
            return;
          }
          continue;
        }

        if (stage === "action" && parsed.Response) {
          result = parsed;
          stage = "done";
          sock.write("Action: Logoff\r\n\r\n");
          // small wait to let logoff flush, then close
          setTimeout(() => finish(null, result), 30);
          return;
        }
      }
    });
  });
}

// ============================================================
// Audio resampling — better quality than naive averages
// ============================================================

// Inbound: slin8 (8kHz) -> slin16 (16kHz) via linear interpolation, stateful.
// 8kHz is already band-limited so linear interp is sufficient. The stateful
// part matters at AudioSocket frame boundaries (every 20ms / 160 samples):
// without state, the interpolated sample at the seam between two frames had
// no "next" reference and degenerated, producing audible clicks Gemini's STT
// reacted to as glitches. Now we carry the last sample across calls.
class Upsampler8to16 {
  private prev: number | null = null;
  process(pcm8: Buffer): Buffer {
    const inSamples = pcm8.length / 2;
    if (inSamples === 0) return Buffer.alloc(0);
    const out = Buffer.alloc(inSamples * 4);
    let oi = 0;
    // First sample: use saved prev if we have one, else duplicate
    const first = pcm8.readInt16LE(0);
    const seamMid = this.prev === null ? first : ((this.prev + first) / 2) | 0;
    out.writeInt16LE(seamMid, oi); oi += 2;       // boundary sample
    out.writeInt16LE(first,   oi); oi += 2;       // first sample of new frame
    for (let i = 1; i < inSamples; i++) {
      const s = pcm8.readInt16LE(i * 2);
      const prev = pcm8.readInt16LE((i - 1) * 2);
      out.writeInt16LE(((prev + s) / 2) | 0, oi); oi += 2;
      out.writeInt16LE(s, oi); oi += 2;
    }
    this.prev = pcm8.readInt16LE((inSamples - 1) * 2);
    return out;
  }
}

// Outbound: slin24 (24kHz) -> slin8 (8kHz) with a 31-tap FIR low-pass + 3:1 decimation.
//
// The previous IIR low-pass (-6 dB/octave) only attenuated 5 kHz content by ~10 dB.
// Sibilant sounds ('s', 'sh' in voice) carry energy up to 8 kHz; after 3:1 decimation
// that 4-8 kHz content folded back into 0-4 kHz, audible to the caller as raspy
// "grr grr" buzz during fricatives. Measured: 8638 single-sample jumps > 0.3 in a
// 153 s call — i.e. ~4 kHz square-wave aliasing during every sibilant.
//
// This FIR is Hamming-windowed sinc, fc = 3700 Hz @ 24 kHz fs. Measured response:
//   0-3 kHz: flat within -1 dB
//   3.4 kHz: -3 dB
//   4.0 kHz: -10 dB
//   4.5 kHz: -22 dB
//   5+  kHz: -47 dB or better
// Cost: 31 multiply-adds per 8 kHz output sample = ~250k MACs/s. Negligible.
class Downsampler24to8 {
  private static readonly KERNEL = [
    +0.001565, +0.001713, +0.000077, -0.003594,
    -0.006319, -0.002548, +0.009117, +0.019419,
    +0.012730, -0.016391, -0.048485, -0.045036,
    +0.022540, +0.142336, +0.259075, +0.307602,
    +0.259075, +0.142336, +0.022540, -0.045036,
    -0.048485, -0.016391, +0.012730, +0.019419,
    +0.009117, -0.002548, -0.006319, -0.003594,
    +0.000077, +0.001713, +0.001565,
  ];
  private static readonly TAPS = Downsampler24to8.KERNEL.length; // 31
  // Sliding history of the last TAPS input samples. Newest at index TAPS-1.
  private history = new Float64Array(Downsampler24to8.TAPS);
  // Decimation phase (0..2) — emit an output sample every time this hits 0.
  private inAccum = 0;

  process(pcm24: Buffer): Buffer {
    const inSamples = pcm24.length / 2;
    const maxOut = Math.ceil(inSamples / 3) + 1;
    const out = Buffer.alloc(maxOut * 2);
    let oi = 0;
    const TAPS = Downsampler24to8.TAPS;
    const K = Downsampler24to8.KERNEL;
    const h = this.history;
    for (let i = 0; i < inSamples; i++) {
      const x = pcm24.readInt16LE(i * 2);
      // shift-left in-place by 1, append new sample at the end
      for (let j = 0; j < TAPS - 1; j++) h[j] = h[j + 1];
      h[TAPS - 1] = x;
      if (this.inAccum === 0) {
        // Convolve history with kernel
        let y = 0;
        for (let k = 0; k < TAPS; k++) y += K[k] * h[k];
        const v = y >= 32767 ? 32767 : y <= -32768 ? -32768 : Math.round(y);
        out.writeInt16LE(v, oi * 2);
        oi++;
      }
      this.inAccum = (this.inAccum + 1) % 3;
    }
    return out.slice(0, oi * 2);
  }
}

// ============================================================
// AudioSocket framing
// ============================================================

function buildAudioFrame(payload: Buffer): Buffer {
  const hdr = Buffer.alloc(3);
  hdr.writeUInt8(0x10, 0);
  hdr.writeUInt16BE(payload.length, 1);
  return Buffer.concat([hdr, payload]);
}

const FRAME_BYTES_8K = 320; // 20ms @ 8kHz slin

// ============================================================
// Working-hours / out-of-hours helpers
// ============================================================
function isWithinWorkingHours(wh?: WorkingHoursConfig): boolean {
  if (!wh || !wh.enabled) return true;
  try {
    const fmt = new Intl.DateTimeFormat("en-GB", {
      timeZone: wh.timezone || "Asia/Colombo",
      weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
    });
    const parts: Record<string, string> = {};
    for (const p of fmt.formatToParts(new Date())) {
      if (p.type !== "literal") parts[p.type] = p.value;
    }
    const dayMap: Record<string, string> = {
      Sun: "0", Mon: "1", Tue: "2", Wed: "3", Thu: "4", Fri: "5", Sat: "6",
    };
    const dow = dayMap[parts.weekday || ""] || "0";
    const hhmm = `${parts.hour}:${parts.minute}`;
    const spec = wh.schedule?.[dow] || "";
    if (!spec) return false;
    // Allow comma-separated ranges: "09:00-12:00,13:00-17:00"
    for (const range of spec.split(",")) {
      const [start, end] = range.split("-").map((s) => s.trim());
      if (start && end && hhmm >= start && hhmm < end) return true;
    }
    return false;
  } catch (_) {
    return true; // fail-open: serve the call normally rather than reject
  }
}

function computeEffectiveTrigger(cfg: AgentConfig, retryMode: boolean): string {
  const baseTrigger = retryMode ? cfg.retry_greeting_trigger : cfg.greeting_trigger;
  const wh = cfg.working_hours;
  if (!wh || !wh.enabled || isWithinWorkingHours(wh)) return baseTrigger;
  const action = wh.out_of_hours_action || "greet";
  if (action === "transfer" && wh.out_of_hours_transfer_category) {
    const msg = wh.out_of_hours_greeting || "We are currently closed. Let me connect you to someone who can help.";
    return `[OUT_OF_HOURS_TRANSFER] Say briefly: "${msg}". Then IMMEDIATELY call request_human_transfer with reason="out_of_hours" and category="${wh.out_of_hours_transfer_category}". Do not engage further.`;
  }
  if (action === "hangup") {
    const msg = wh.out_of_hours_hangup_message || "We are currently closed. Please call back during business hours.";
    return `[OUT_OF_HOURS_HANGUP] Say exactly: "${msg}". Then IMMEDIATELY call end_call with reason="out_of_hours". Do not engage further.`;
  }
  const msg = wh.out_of_hours_greeting || "Thanks for calling. We are currently outside our normal business hours but I will do my best to help.";
  return `[OUT_OF_HOURS_GREET] Greet the caller with: "${msg}" and continue to help with whatever you can.`;
}

const sleep = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Wait for the agent to finish its current/next spoken turn before we run a
 * call-ending action (hangup / redirect). Without this, an `end_call` or
 * `request_human_transfer` tool call cuts off the agent mid-goodbye.
 *
 * `markerMs` should be the moment we sent the tool response — we wait for a
 * turn_complete fired AFTER that point (so we don't return on a stale event).
 *
 * Done when ALL of:
 *   - Gemini fired a fresh turn_complete  OR  audio has been silent for >800ms
 *   - The bridge's outbound buffer to Asterisk has fully drained
 *   - A 300ms safety pad has elapsed (lets Asterisk play the last queued frame)
 *
 * Bounded by maxWaitMs so a missing turn_complete never blocks forever.
 */
async function waitForAgentToFinish(
  state: BridgeState,
  markerMs: number,
  maxWaitMs = 12000
): Promise<void> {
  const start = Date.now();
  // Phase 1: wait for Gemini to stop generating (turn_complete OR audio silence).
  while (Date.now() - start < maxWaitMs) {
    if (state.closed) return;
    const sawTurnComplete = state.lastTurnCompleteAt > markerMs;
    const hadAudio = state.lastGeminiAudioAt > 0;
    const sinceAudio = Date.now() - state.lastGeminiAudioAt;
    const enoughSilence = hadAudio && sinceAudio > 800;
    if (sawTurnComplete || enoughSilence) break;
    await sleep(100);
  }
  // Phase 2: wait for the pace timer to flush the outbound buffer to Asterisk.
  while (
    Date.now() - start < maxWaitMs &&
    state.pendingOutbound.length > 0
  ) {
    await sleep(50);
  }
  // Phase 3: safety pad — Asterisk's own queue still has the last frame or two.
  await sleep(300);
}

// ============================================================
// Session event log
// ============================================================

function nowIso() {
  return new Date().toISOString();
}

// Booking tools must NOT save until the agent has actually collected a real name
// + phone from the caller. These guards reject placeholder/guessed values so the
// agent is forced to ask (no silent fallback to the caller's channel number).
function validPhone(s: string): boolean {
  return String(s || "").replace(/\D/g, "").length >= 7;
}
function validName(s: string): boolean {
  const n = String(s || "").trim();
  if (n.length < 2) return false;
  return ![
    "patient", "customer", "guest", "unknown", "caller", "n/a", "na", "none",
    "test", "sir", "madam", "anonymous", "mr", "mrs", "ms", "x", "name",
  ].includes(n.toLowerCase());
}

function appendSessionEvent(uuid: string, event: Record<string, unknown>) {
  if (!uuid) return;
  try {
    const file = path.join(SESSIONS_DIR, `${uuid}.jsonl`);
    fs.appendFileSync(
      file,
      JSON.stringify({ ts: nowIso(), ...event }) + "\n"
    );
  } catch (e) {
    console.warn("[session] log write failed:", (e as Error).message);
  }
}

function saveCustomerInfo(callerKey: string, field: string, value: string) {
  if (!callerKey || !field) return;
  try {
    const file = path.join(CUSTOMERS_DIR, `${callerKey}.json`);
    let data: Record<string, unknown> = {};
    if (fs.existsSync(file)) {
      try {
        data = JSON.parse(fs.readFileSync(file, "utf-8"));
      } catch (_) {
        data = {};
      }
    }
    if (!data.fields) data.fields = {};
    (data.fields as Record<string, unknown>)[field] = value;
    data.last_updated = nowIso();
    fs.writeFileSync(file, JSON.stringify(data, null, 2));
  } catch (e) {
    console.warn(
      "[customer] save failed:",
      (e as Error).message
    );
  }
}

function lookupChannelName(uuid: string): string | null {
  try {
    const p = path.join(CHANNEL_REGISTRY_DIR, `${uuid}.chan`);
    if (!fs.existsSync(p)) return null;
    return fs.readFileSync(p, "utf-8").trim();
  } catch (_) {
    return null;
  }
}

// ============================================================
// Hospital booking helpers
//   - reference ("dummy") directory of doctors/branches/specialties:
//     /var/lib/sampath-ai/refdata/hospital.json  (shared with the dashboard)
//   - real bookings captured from calls, one JSON file per booking:
//     /var/lib/sampath-ai/bookings/hospital/<id>.json
// ============================================================
interface RefDoctor {
  id?: string; name: string; specialty: string; branches: string[];
  fee?: number; days?: string[]; slots?: string[];
}
interface HospitalRef {
  currency?: string; branches: string[];
  specialties: Array<{ name: string; keywords?: string[] }>;
  doctors: RefDoctor[];
}

let _hospitalRef: HospitalRef | null = null;
let _hospitalRefMtime = 0;
function loadHospitalRef(): HospitalRef {
  const file = path.join(REFDATA_DIR, "hospital.json");
  try {
    const st = fs.statSync(file);
    if (!_hospitalRef || st.mtimeMs !== _hospitalRefMtime) {
      _hospitalRef = JSON.parse(fs.readFileSync(file, "utf-8")) as HospitalRef;
      _hospitalRefMtime = st.mtimeMs;
    }
  } catch (_) {
    if (!_hospitalRef) _hospitalRef = { branches: [], specialties: [], doctors: [] };
  }
  return _hospitalRef!;
}

function resolveSpecialty(ref: HospitalRef, text: string): string | null {
  const t = (text || "").toLowerCase().trim();
  if (!t) return null;
  const direct = ref.specialties.find(
    (s) => s.name.toLowerCase() === t || t.includes(s.name.toLowerCase())
  );
  if (direct) return direct.name;
  for (const s of ref.specialties) {
    if ((s.keywords || []).some((k) => t.includes(k.toLowerCase()))) return s.name;
  }
  return null;
}

function findDoctors(
  ref: HospitalRef,
  opts: { symptom?: string; specialty?: string; branch?: string; doctor?: string }
): RefDoctor[] {
  let docs = ref.doctors.slice();
  if (opts.doctor) {
    const dq = opts.doctor.toLowerCase();
    docs = docs.filter((d) => d.name.toLowerCase().includes(dq));
  }
  const spec = opts.specialty
    ? resolveSpecialty(ref, opts.specialty) || opts.specialty
    : opts.symptom
    ? resolveSpecialty(ref, opts.symptom)
    : null;
  if (spec) docs = docs.filter((d) => d.specialty.toLowerCase() === spec.toLowerCase());
  if (opts.branch) {
    const bq = opts.branch.toLowerCase();
    docs = docs.filter((d) =>
      (d.branches || []).some((b) => b.toLowerCase().includes(bq) || bq.includes(b.toLowerCase()))
    );
  }
  return docs;
}

// Durdans appointment reference, e.g. DUR-AP-482917 — phone-friendly 6 digits,
// matching the brand the agent reads back. Retry on the off chance the file exists.
function makeBookingId(): string {
  const dir = path.join(BOOKINGS_DIR, "hospital", "appointments");
  for (let i = 0; i < 50; i++) {
    const id = "DUR-AP-" + Math.floor(100000 + Math.random() * 900000);
    try {
      if (!fs.existsSync(path.join(dir, `${id}.json`))) return id;
    } catch (_) {
      return id;
    }
  }
  return "DUR-AP-" + Math.floor(100000 + Math.random() * 900000);
}

// Channelling queue position — the patient's number for this doctor's clinic at
// this branch on this date (1-based). Demo data reuses doctor names across
// branches, so the queue is scoped to doctor + branch + date. Counts every
// existing appointment file that matches (cancelled / no-shows excluded).
function nextQueueNo(doctor: string, branch: string, date: string): number {
  const dir = path.join(BOOKINGS_DIR, "hospital", "appointments");
  const dn = String(doctor || "").toLowerCase().trim();
  const bn = String(branch || "").toLowerCase().trim();
  let n = 0;
  try {
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith(".json")) continue;
      try {
        const a = JSON.parse(fs.readFileSync(path.join(dir, f), "utf-8"));
        if (["cancelled", "no_show"].includes(a.status)) continue;
        if (
          String(a.doctor || "").toLowerCase().trim() === dn &&
          String(a.branch || "").toLowerCase().trim() === bn &&
          String(a.date || "") === String(date || "")
        )
          n++;
      } catch (_) {}
    }
  } catch (_) {}
  return n + 1;
}

function writeBooking(rec: Record<string, unknown>) {
  const dir = path.join(BOOKINGS_DIR, "hospital", "appointments");
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (_) {}
  const file = path.join(dir, `${rec.id}.json`);
  const tmp = file + ".tmp";                 // atomic write: the dashboard polls this dir
  fs.writeFileSync(tmp, JSON.stringify(rec, null, 2));
  fs.renameSync(tmp, file);
}

// Generic reference-data loader + record writer (used by verticals beyond hospital).
const _refCache: Record<string, { mtime: number; data: any }> = {};
function loadRef(name: string): any {
  const file = path.join(REFDATA_DIR, name + ".json");
  try {
    const st = fs.statSync(file);
    const c = _refCache[name];
    if (!c || c.mtime !== st.mtimeMs) {
      _refCache[name] = { mtime: st.mtimeMs, data: JSON.parse(fs.readFileSync(file, "utf-8")) };
    }
  } catch (_) {
    if (!_refCache[name]) _refCache[name] = { mtime: 0, data: {} };
  }
  return _refCache[name].data;
}
function makeId(prefix: string): string {
  const stamp = new Date().toISOString().slice(2, 10).replace(/-/g, "");
  const rand = Math.random().toString(36).slice(2, 6).toUpperCase();
  return `${prefix}-${stamp}-${rand}`;
}
function writeRecord(vertical: string, collection: string, rec: Record<string, unknown>) {
  const dir = path.join(BOOKINGS_DIR, vertical, collection);
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (_) {}
  const file = path.join(dir, `${rec.id}.json`);
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(rec, null, 2));
  fs.renameSync(tmp, file);
}

// Live stock for the sales catalogue = product.stock minus quantities already
// committed in non-cancelled orders (the shop computes this the same way).
function salesReserved(): Record<string, number> {
  const dir = path.join(BOOKINGS_DIR, "sales", "orders");
  const res: Record<string, number> = {};
  try {
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith(".json")) continue;
      try {
        const o = JSON.parse(fs.readFileSync(path.join(dir, f), "utf-8"));
        if (o.status === "cancelled") continue;
        for (const ln of (o.lines || [])) {
          if (ln && ln.sku) res[ln.sku] = (res[ln.sku] || 0) + (Number(ln.qty) || 0);
        }
      } catch (_) {}
    }
  } catch (_) {}
  return res;
}

// Match a lab test/panel from the hospital catalogue by code, name or keyword.
function matchTest(query: string): any {
  const ref = loadRef("hospital");
  const tests = ref.tests || [];
  const q = (query || "").toLowerCase().trim();
  if (!q) return null;
  return (
    tests.find((t: any) => String(t.code).toLowerCase() === q || String(t.name).toLowerCase().includes(q)) ||
    tests.find((t: any) => (t.keywords || []).some((k: string) => q.includes(String(k).toLowerCase()) || String(k).toLowerCase().includes(q))) ||
    null
  );
}

// Real patient registry: dedupe by phone, else create an MRN record.
// Returns the patient's MRN. Used by book_appointment + order_lab_test.
function upsertPatient(name: string, phone: string, extra?: Record<string, unknown>): string {
  const dir = path.join(BOOKINGS_DIR, "hospital", "patients");
  try { fs.mkdirSync(dir, { recursive: true }); } catch (_) {}
  const norm = (s: string) => String(s || "").replace(/[^0-9+]/g, "");
  const pn = norm(phone);
  try {
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith(".json")) continue;
      try {
        const p = JSON.parse(fs.readFileSync(path.join(dir, f), "utf-8"));
        if (pn && norm(p.phone) === pn) {
          let changed = false;
          if ((!p.name || p.name === "Unknown") && name) { p.name = name; changed = true; }
          if (extra) for (const k in extra) { if (p[k] == null && (extra as any)[k] != null) { p[k] = (extra as any)[k]; changed = true; } }
          if (changed) { const t = path.join(dir, f) + ".tmp"; fs.writeFileSync(t, JSON.stringify(p, null, 2)); fs.renameSync(t, path.join(dir, f)); }
          return p.id || p.mrn || f.replace(/\.json$/, "");
        }
      } catch (_) {}
    }
  } catch (_) {}
  const mrn = "MRN" + Math.floor(10000 + Math.random() * 89999);
  const rec: Record<string, unknown> = { id: mrn, mrn, name: name || "Unknown", phone, created: nowIso(), source: "AI call", ...(extra || {}) };
  const file = path.join(dir, `${mrn}.json`);
  const tmp = file + ".tmp"; fs.writeFileSync(tmp, JSON.stringify(rec, null, 2)); fs.renameSync(tmp, file);
  return mrn;
}

// Outbound confirm-call context — written by the app before originating, keyed by
// the same UUID Asterisk passes over AudioSocket on port 9092.
function loadOutboundContext(uuid: string): any {
  try {
    return JSON.parse(fs.readFileSync(path.join(OUTBOUND_DIR, `${uuid}.json`), "utf-8"));
  } catch (_) {
    return null;
  }
}

function buildOutboundConfig(ctx: any): AgentConfig {
  const base = loadAgentConfig();  // reuse model/voice/voices; override persona + greeting
  const kind = (ctx && ctx.kind) || "order_confirm";
  const who = (ctx && ctx.customer) || "the customer";
  const summary = (ctx && ctx.summary) || "";
  const ref = (ctx && ctx.ref) || "";
  const cur = (ctx && ctx.currency) || "Rs";
  let system_prompt: string, greeting_trigger: string, tools: string[];

  if (kind === "appt_confirm" || kind === "appt_reminder") {
    system_prompt =
      "You are a polite assistant from a hospital making an OUTBOUND call to confirm or remind a patient about an appointment. Be brief, warm and clear. Detect and speak the patient's language (English / Sinhala / Tamil). Discuss only this appointment.";
    greeting_trigger =
      `You have just called ${who}. Greet them, say you are calling from the hospital about their appointment: ${summary}. Ask them to confirm it still suits them. ` +
      `If they confirm, call call_outcome with outcome "confirmed". If they want to cancel, outcome "cancelled". If they want a different time, outcome "reschedule" with a note. Then thank them and call end_call.`;
    tools = ["call_outcome", "end_call"];
  } else if (kind === "lab_critical") {
    system_prompt =
      "You are a careful clinical assistant from a hospital laboratory making an URGENT OUTBOUND call about an important (critical) test result. Be calm, clear and serious. Advise the patient to contact their doctor or come to the hospital promptly. Ask them to repeat back what they should do. Speak their language. Do NOT give detailed medical advice beyond advising prompt follow-up.";
    greeting_trigger =
      `You have just called ${who}. Greet them, say you are calling from the hospital laboratory about their test ${summary} (reference ${ref}) and that the result needs prompt attention. Advise them to see their doctor or come to the hospital as soon as possible, and ask them to read back / confirm they understood. ` +
      `When they acknowledge, call call_outcome with outcome "acknowledged" and a brief note. Then thank them and call end_call.`;
    tools = ["call_outcome", "end_call"];
  } else if (kind === "lab_ready") {
    system_prompt =
      "You are a polite assistant from a hospital laboratory making an OUTBOUND call to tell a patient their lab results are ready. Be brief and friendly. Speak their language.";
    greeting_trigger =
      `You have just called ${who}. Greet them, say you are calling from the hospital laboratory to let them know their results for ${summary} (reference ${ref}) are ready to collect or be sent. Confirm they heard. ` +
      `Call call_outcome with outcome "confirmed". Then thank them and call end_call.`;
    tools = ["call_outcome", "end_call"];
  } else {
    // sales order_confirm (default — unchanged behaviour)
    const total = ctx && ctx.total != null ? `, total ${cur} ${ctx.total}` : "";
    const pay = ctx && ctx.payment ? `, payment ${ctx.payment}` : "";
    system_prompt =
      "You are a polite assistant from an online store making an OUTBOUND call to a customer to confirm an order they placed. Be brief, warm and clear. Detect and speak the customer's language (English / Sinhala / Tamil). Do not discuss anything unrelated to this order.";
    greeting_trigger =
      `You have just called ${who}. Greet them, say you are calling from the store to confirm their order ${ref}: ${summary || "their recent order"}${total}${pay}. Read the order back and ask them to confirm it is correct and to confirm the delivery address. ` +
      `When they confirm, call confirm_order with outcome "confirmed". If they want to cancel, call it with outcome "cancelled". If they want changes or a callback later, call it with outcome "reschedule" and a short note. After that, thank them and call end_call.`;
    tools = ["confirm_order", "end_call"];
  }
  return { ...base, system_prompt, custom_instructions: "", greeting_trigger, retry_greeting_trigger: "", tools_enabled: tools, working_hours: undefined };
}

// ============================================================
// Per-connection bridge
// ============================================================

interface BridgeState {
  sock: net.Socket;
  callerUuid: string;
  channelName: string | null;
  callerNum: string;
  gemini: GeminiLiveSession | null;
  pendingOutbound: Buffer;
  upsampler: Upsampler8to16;
  downsampler: Downsampler24to8;
  paceTimer: NodeJS.Timeout | null;
  draining: boolean;  // true once jitter buffer has primed and we're streaming
  retryMode: boolean;
  orderRef?: string;        // outbound calls: the record being acted on
  orderVertical?: string;
  outboundCtx?: any;        // full outbound context (kind/collection/ref/...)
  closed: boolean;
  escalating: boolean;
  lastFrameAt: number;  // ms; last time we wrote ANY frame to Asterisk
  lastGeminiAudioAt: number;  // ms; last time Gemini sent us audio (0 if never)
  lastTurnCompleteAt: number;  // ms; last time Gemini fired turn_complete
}

function makeHandler(retryMode: boolean, outbound = false) {
  return (sock: net.Socket) => handleConnection(sock, retryMode, outbound);
}

function handleConnection(sock: net.Socket, retryMode: boolean, outbound = false) {
  console.log(
    `[bridge] connection from ${sock.remoteAddress}:${sock.remotePort} (retry=${retryMode} outbound=${outbound})`
  );

  const state: BridgeState = {
    sock,
    callerUuid: "",
    channelName: null,
    callerNum: "",
    gemini: null,
    pendingOutbound: Buffer.alloc(0),
    upsampler: new Upsampler8to16(),
    downsampler: new Downsampler24to8(),
    paceTimer: null,
    draining: false,
    retryMode,
    closed: false,
    escalating: false,
    lastFrameAt: Date.now(),
    lastGeminiAudioAt: 0,
    lastTurnCompleteAt: 0,
  };

  // AudioSocket keepalive: Asterisk's app_audiosocket drops the connection
  // after 2000ms with no inbound frames. When Gemini is silent (waiting for
  // the caller to speak), the pace timer below has nothing to send and the
  // call dies after 2s. A 320-byte zero buffer = 20ms of slin8 silence.
  const SILENCE_FRAME = buildAudioFrame(Buffer.alloc(FRAME_BYTES_8K));
  const KEEPALIVE_MAX_GAP_MS = 500;  // well under the 2000ms Asterisk timeout

  let buf = Buffer.alloc(0);

  // Pace outbound audio to Asterisk at 50 fps (one 20-ms frame per tick),
  // with a jitter-buffer prime of 3 frames (60 ms).
  //
  // Why pacing: Gemini Live streams TTS in bursts (5s of audio in ~2s). If we
  // dump it all into AudioSocket at once, Asterisk's channel queue balloons
  // and Samali keeps talking 2-3 seconds after the caller starts speaking,
  // breaking barge-in. Pacing keeps Asterisk's queue at 1 frame deep so
  // barge-in is effectively immediate.
  //
  // Why a 60ms prime: Gemini's chunk arrival is jittery — chunks land in 50-
  // 130 ms bursts followed by 20-80 ms gaps. With a flat 1-frame pace, those
  // gaps caused audible mid-sentence stutters (54 micro-gaps measured per
  // 60-second call). The 3-frame prime gives the channel a small safety
  // buffer to ride through inter-chunk gaps. Cost: 60 ms of extra start
  // latency per response, which is well under human perception threshold.
  const PRIME_FRAMES = 3;
  state.paceTimer = setInterval(() => {
    if (state.closed) return;
    const queueBytes = state.pendingOutbound.length;
    const writeKeepalive = () => {
      if (Date.now() - state.lastFrameAt < KEEPALIVE_MAX_GAP_MS) return;
      try {
        state.sock.write(SILENCE_FRAME);
        state.lastFrameAt = Date.now();
      } catch (e) {
        console.error("[bridge] keepalive write failed:", (e as Error).message);
      }
    };
    if (!state.draining) {
      // Priming: wait until we've buffered PRIME_FRAMES worth before draining.
      if (queueBytes < FRAME_BYTES_8K * PRIME_FRAMES) { writeKeepalive(); return; }
      state.draining = true;
    }
    if (queueBytes < FRAME_BYTES_8K) {
      // Drained dry — re-enter priming so the next response gets a fresh buffer.
      state.draining = false;
      writeKeepalive();
      return;
    }
    const chunk = state.pendingOutbound.slice(0, FRAME_BYTES_8K);
    state.pendingOutbound = state.pendingOutbound.slice(FRAME_BYTES_8K);
    try {
      state.sock.write(buildAudioFrame(chunk));
      state.lastFrameAt = Date.now();
    } catch (e) {
      console.error("[bridge] write failed:", (e as Error).message);
    }
  }, 20);

  sock.on("data", async (chunk) => {
    buf = Buffer.concat([buf, chunk]);

    while (buf.length >= 3) {
      const type = buf.readUInt8(0);
      const len = buf.readUInt16BE(1);
      if (buf.length < 3 + len) break;
      const payload = buf.slice(3, 3 + len);
      buf = buf.slice(3 + len);

      if (type === 0x01) {
        // UUID frame — first thing Asterisk sends
        state.callerUuid = payload.toString("hex");
        const formattedUuid =
          state.callerUuid.length === 32
            ? `${state.callerUuid.slice(0, 8)}-${state.callerUuid.slice(
                8,
                12
              )}-${state.callerUuid.slice(12, 16)}-${state.callerUuid.slice(
                16,
                20
              )}-${state.callerUuid.slice(20)}`
            : state.callerUuid;
        console.log(`[bridge] UUID = ${formattedUuid}`);

        state.channelName = lookupChannelName(formattedUuid);
        console.log(`[bridge] channel = ${state.channelName || "(unknown)"}`);

        appendSessionEvent(formattedUuid, {
          type: "session_open",
          mode: outbound ? "outbound" : retryMode ? "retry" : "primary",
          channel: state.channelName,
        });

        let cfg: AgentConfig;
        if (outbound) {
          const octx = loadOutboundContext(formattedUuid);
          state.outboundCtx = octx;
          state.orderRef = octx && octx.ref ? String(octx.ref) : undefined;
          state.orderVertical = (octx && octx.vertical) ? String(octx.vertical) : "sales";
          cfg = buildOutboundConfig(octx);
        } else {
          cfg = loadAgentConfig();
        }

        try {
          state.gemini = await createGeminiLiveSession(
            cfg,
            formattedUuid,
            retryMode
          );
          console.log("[bridge] Gemini session open");
          appendSessionEvent(formattedUuid, {
            type: "gemini_ready",
            voice: cfg.voice,
            model: cfg.model,
          });

          state.gemini.onAudio((b64) => {
            const pcm24 = Buffer.from(b64, "base64");
            const pcm8 = state.downsampler.process(pcm24);
            state.pendingOutbound = Buffer.concat([
              state.pendingOutbound,
              pcm8,
            ]);
            state.lastGeminiAudioAt = Date.now();
            // pace timer drains pendingOutbound at 50 fps — see comment above
          });

          state.gemini.onTranscript((text, role) => {
            const trimmed = (text || "").trim();
            if (!trimmed) return;
            console.log(`[bridge] ${role}: ${trimmed.slice(0, 160)}`);
            appendSessionEvent(formattedUuid, {
              type: "transcript",
              role,
              text: trimmed,
            });
          });

          state.gemini.onInterrupt(() => {
            // Caller barged in — drop any queued outbound audio so we don't
            // keep playing over them, and re-arm the jitter buffer so the
            // next response gets a clean 60ms prime.
            state.pendingOutbound = Buffer.alloc(0);
            state.draining = false;
            appendSessionEvent(formattedUuid, { type: "interrupted" });
          });

          state.gemini.onTurnComplete(() => {
            state.lastTurnCompleteAt = Date.now();
            appendSessionEvent(formattedUuid, { type: "turn_complete" });
          });

          state.gemini.onToolCall((call: ToolCallEvent) => {
            handleToolCall(state, formattedUuid, cfg, call);
          });

          state.gemini.onError((err) => {
            console.error("[bridge] gemini error:", err);
            appendSessionEvent(formattedUuid, { type: "error", error: err });
          });

          state.gemini.onClose(() => {
            console.log("[bridge] gemini session closed");
            appendSessionEvent(formattedUuid, { type: "gemini_closed" });
            close(state, "gemini closed");
          });

          // Send the appropriate greeting trigger — overridden by working_hours
          // config when the call lands outside business hours (action: greet/transfer/hangup).
          const trigger = computeEffectiveTrigger(cfg, retryMode);
          const inHours = isWithinWorkingHours(cfg.working_hours);
          appendSessionEvent(formattedUuid, {
            type: "trigger_chosen",
            in_hours: inHours,
            action: cfg.working_hours?.enabled && !inHours ? (cfg.working_hours.out_of_hours_action || "greet") : "in_hours",
          });
          state.gemini.sendText(trigger);

          // Best-effort: flag the channel for recording if the flow asks for it.
          // The dialplan can decide what to do with AI_RECORD (e.g. MixMonitor).
          if (cfg.record_calls && state.channelName) {
            try {
              await amiAction({
                Action: "Setvar",
                Channel: state.channelName,
                Variable: "AI_RECORD",
                Value: "1",
              });
              appendSessionEvent(formattedUuid, { type: "ami_setvar", variable: "AI_RECORD", value: "1" });
            } catch (e) {
              console.warn("[bridge] AMI Setvar AI_RECORD failed:", (e as Error).message);
            }
          }
        } catch (err) {
          console.error("[bridge] Gemini setup failed:", err);
          appendSessionEvent(formattedUuid, {
            type: "error",
            error: (err as Error).message,
          });
          close(state, "gemini setup failed");
        }
      } else if (type === 0x10) {
        // Audio frame from Asterisk = slin8 @ 8kHz
        if (!state.gemini) continue;
        const upsampled = state.upsampler.process(payload);
        state.gemini.send(upsampled.toString("base64"));
      } else if (type === 0x00) {
        console.log("[bridge] hangup received from Asterisk");
        appendSessionEvent(state.callerUuid, { type: "hangup_from_asterisk" });
        close(state, "asterisk hangup");
      } else if (type === 0xff) {
        console.error(
          "[bridge] error frame from Asterisk:",
          payload.toString("hex")
        );
      }
    }
  });

  sock.on("close", () => {
    if (state.escalating) {
      console.log("[bridge] socket closed (escalating, expected)");
    } else {
      console.log("[bridge] socket closed");
    }
    close(state, state.escalating ? "escalating" : "tcp close");
  });

  sock.on("error", (err) => {
    console.error("[bridge] socket error:", err.message);
    close(state, "tcp error");
  });
}

// ============================================================
// Tool handlers
// ============================================================

async function handleToolCall(
  state: BridgeState,
  uuid: string,
  cfg: AgentConfig,
  call: ToolCallEvent
) {
  const { name, args, id } = call;
  appendSessionEvent(uuid, {
    type: "tool_call",
    name,
    args,
  });

  try {
    if (name === "find_sampath_branch") {
      const query = String(args.query || "").trim();
      const matches = findBranches(query, 4).map(formatBranchForAgent);
      appendSessionEvent(uuid, {
        type: "lookup",
        kind: "branch",
        query,
        count: matches.length,
      });
      state.gemini?.sendToolResponse(id, name, {
        query,
        count: matches.length,
        matches,
        note: matches.length === 0
          ? "No matching branches found in the live database. Apologise to the customer and ask them to clarify the location, or suggest they call the main hotline 011-2-303-050."
          : "These are LIVE results pulled from sampath.lk's branch database. Read the relevant details to the customer.",
      });
      return;
    }

    if (name === "get_exchange_rates") {
      const currency = args.currency ? String(args.currency).trim() : undefined;
      const matches = getRates(currency).map(formatRateForAgent);
      appendSessionEvent(uuid, {
        type: "lookup",
        kind: "rates",
        currency: currency || "(all)",
        count: matches.length,
      });
      state.gemini?.sendToolResponse(id, name, {
        currency: currency || null,
        count: matches.length,
        rates: matches.slice(0, currency ? 3 : 17),
        note: "These are LIVE rates from sampath.lk effective from the timestamp shown. The bank BUYS your foreign currency at TTBUY and SELLS to you at TTSEL. Always mention the effective_from timestamp.",
      });
      return;
    }

    if (name === "save_customer_info") {
      const field = String(args.field || "").trim();
      const value = String(args.value || "").trim();
      // Use the channel name (or uuid) as the customer key — caller IDs
      // are forwarded into the channel registry file by dialplan.
      // Fall back to uuid if we don't have anything more specific.
      const customerKey = state.callerNum || uuid;
      saveCustomerInfo(customerKey, field, value);
      appendSessionEvent(uuid, {
        type: "extracted",
        field,
        value,
        customer_key: customerKey,
      });
      state.gemini?.sendToolResponse(id, name, { ok: true, saved: { field, value } });
      return;
    }

    if (name === "request_human_transfer") {
      const reason = String(args.reason || "").trim();
      const category = String(args.category || "default").trim() || "default";

      // Look up the matching transfer rule. The bridge picks the manager number
      // here (instead of letting the dialplan re-read agent-config.json) so the
      // active flow's per-category routing applies on a per-call basis.
      const rules = cfg.transfer_rules || [];
      const matched = rules.find((r) => r.category === category)
        || rules.find((r) => r.category === "default")
        || rules[0];
      const targetNumber = (matched?.manager_number || cfg.manager_number || "").trim();

      appendSessionEvent(uuid, {
        type: "escalation_requested",
        reason,
        category,
        matched_category: matched?.category || null,
        target_number: targetNumber,
      });
      state.gemini?.sendToolResponse(id, name, {
        ok: true,
        status: "transferring",
        category: matched?.category || "default",
      });

      if (!state.channelName) {
        console.error(
          "[bridge] cannot escalate — no channel name registered for uuid",
          uuid
        );
        appendSessionEvent(uuid, {
          type: "escalation_failed",
          error: "no_channel_name",
        });
        return;
      }

      // Wait for the agent to finish its "please hold while I transfer you"
      // announcement before we redirect. See waitForAgentToFinish() above.
      const transferMarker = Date.now();
      (async () => {
        await waitForAgentToFinish(state, transferMarker);
        if (state.closed) return;
        state.escalating = true;

        // Push the chosen manager number into a channel variable BEFORE the
        // Redirect, so [ai-escalate] picks it up via ${AI_MGR_NUMBER}. The
        // dialplan still has its jq-based fallback so a Setvar failure won't
        // strand the call.
        if (targetNumber) {
          try {
            await amiAction({
              Action: "Setvar",
              Channel: state.channelName!,
              Variable: "AI_MGR_NUMBER",
              Value: targetNumber,
            });
            appendSessionEvent(uuid, {
              type: "ami_setvar",
              variable: "AI_MGR_NUMBER",
              value: targetNumber,
            });
          } catch (e) {
            console.warn(
              "[bridge] AMI Setvar AI_MGR_NUMBER failed (dialplan will fall back):",
              (e as Error).message
            );
          }
        }

        console.log(
          `[bridge] redirecting ${state.channelName} to ai-escalate (category=${matched?.category || "default"}, mgr=${targetNumber || "fallback"})`
        );
        try {
          const r = await amiAction({
            Action: "Redirect",
            Channel: state.channelName!,
            Context: "ai-escalate",
            Exten: "s",
            Priority: "1",
          });
          appendSessionEvent(uuid, {
            type: "ami_redirect",
            target: "ai-escalate",
            response: r.Response,
          });
        } catch (e) {
          console.error("[bridge] AMI Redirect failed:", (e as Error).message);
          appendSessionEvent(uuid, {
            type: "escalation_failed",
            error: (e as Error).message,
          });
        }
      })();
      return;
    }

    if (name === "end_call") {
      const reason = String(args.reason || "").trim();
      appendSessionEvent(uuid, { type: "end_call_requested", reason });
      state.gemini?.sendToolResponse(id, name, { ok: true });

      // Wait for the agent's spoken goodbye to finish before hanging up.
      // See waitForAgentToFinish() above.
      const endMarker = Date.now();
      (async () => {
        await waitForAgentToFinish(state, endMarker);
        if (state.closed) return;
        if (!state.channelName) {
          // Fallback — just close gemini and let socket close naturally
          close(state, "end_call (no channel)");
          return;
        }
        try {
          await amiAction({
            Action: "Hangup",
            Channel: state.channelName,
          });
          appendSessionEvent(uuid, { type: "ami_hangup", channel: state.channelName });
        } catch (e) {
          console.error("[bridge] AMI Hangup failed:", (e as Error).message);
          appendSessionEvent(uuid, {
            type: "ami_hangup_failed",
            error: (e as Error).message,
          });
        }
      })();
      return;
    }

    if (name === "find_doctor") {
      const ref = loadHospitalRef();
      const opts = {
        symptom: args.symptom ? String(args.symptom) : undefined,
        specialty: args.specialty ? String(args.specialty) : undefined,
        branch: args.branch ? String(args.branch) : undefined,
        doctor: args.doctor ? String(args.doctor) : undefined,
      };
      // DEMO: always offer someone — we never tell the caller "no doctor".
      // If nothing matched, relax the filters step by step (drop the doctor-name
      // filter, then the branch) and finally fall back to General Medicine / any
      // doctor in the directory.
      let found = findDoctors(ref, opts);
      if (!found.length && opts.doctor) found = findDoctors(ref, { ...opts, doctor: undefined });
      if (!found.length && opts.branch) found = findDoctors(ref, { symptom: opts.symptom, specialty: opts.specialty });
      if (!found.length) found = findDoctors(ref, { specialty: "General Medicine", branch: opts.branch });
      if (!found.length) found = findDoctors(ref, { specialty: "General Medicine" });
      if (!found.length) found = ref.doctors.slice();
      const matches = found.slice(0, 4).map((d) => ({
        name: d.name, specialty: d.specialty, branches: d.branches,
        fee: d.fee, days: d.days, times: d.slots,
      }));
      appendSessionEvent(uuid, { type: "lookup", kind: "doctor", query: opts, count: matches.length });
      state.gemini?.sendToolResponse(id, name, {
        count: matches.length,
        doctors: matches,
        branches: ref.branches,
        note: "These are the available doctors. Offer one or two to the caller (name, branch, fee, a couple of times) and let them choose before you call book_appointment.",
      });
      return;
    }

    if (name === "book_appointment") {
      const ref = loadHospitalRef();
      const patient = String(args.patient_name || "").trim();
      const phone = String(args.phone || "").trim();
      const date = String(args.date || "").trim();
      const time = String(args.time || "").trim();
      const reason = String(args.reason || "").trim();
      let branch = String(args.branch || "").trim();
      const docName = String(args.doctor || "").trim();
      const specialtyArg = String(args.specialty || "").trim();

      if (!validName(patient) || !validPhone(phone) || !date || !time) {
        state.gemini?.sendToolResponse(id, name, {
          ok: false, error: "need_details",
          message: "Do NOT book yet. First ASK the caller for their full name and a contact phone number (and the date and time), and use EXACTLY what they tell you — never guess, assume, or make up a name or number. Once the caller has given you their real name and phone, call book_appointment again.",
        });
        return;
      }

      // Resolve a real doctor from the directory (by name, else by specialty/symptom + branch).
      let doc = docName
        ? ref.doctors.find((d) => d.name.toLowerCase().includes(docName.toLowerCase()))
        : undefined;
      if (!doc) {
        const spec = resolveSpecialty(ref, specialtyArg || reason || "");
        doc = findDoctors(ref, { specialty: spec || specialtyArg || undefined, branch: branch || undefined })[0];
      }
      // DEMO: never fail to book for lack of a doctor — fall back to General
      // Medicine (in the requested branch, then anywhere), then any doctor.
      if (!doc) doc = findDoctors(ref, { specialty: "General Medicine", branch: branch || undefined })[0];
      if (!doc) doc = findDoctors(ref, { specialty: "General Medicine" })[0];
      if (!doc) doc = ref.doctors[0];
      if (!doc) {
        // Only reachable if the doctor directory is empty/unreadable.
        state.gemini?.sendToolResponse(id, name, {
          ok: false, error: "save_failed",
          message: "The doctor directory is unavailable right now. Take the caller's name and number and connect them to a representative.",
        });
        return;
      }
      // Ensure the branch is one this doctor actually serves.
      if (!branch || !(doc.branches || []).some((b) => b.toLowerCase() === branch.toLowerCase())) {
        branch = (doc.branches && doc.branches[0]) || branch || "Colombo";
      }
      const bid = makeBookingId();
      const queueNo = nextQueueNo(doc.name, branch, date);
      const rec = {
        id: bid, ref: bid, patient, phone, doctor: doc.name, specialty: doc.specialty,
        branch, date, time, reason, type: "Consultation", fee: doc.fee || 0, queue_no: queueNo,
        status: "booked", source: "AI call", paid: false, call_uuid: uuid, created: nowIso(),
      };
      try {
        writeBooking(rec);
      } catch (e) {
        console.error("[bridge] booking write failed:", (e as Error).message);
        appendSessionEvent(uuid, { type: "booking_failed", error: (e as Error).message });
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "save_failed" });
        return;
      }
      upsertPatient(patient, phone);
      appendSessionEvent(uuid, {
        type: "booking_created", booking_id: bid, patient,
        doctor: doc.name, specialty: doc.specialty, branch, date, time, queue_no: queueNo,
      });
      state.gemini?.sendToolResponse(id, name, {
        ok: true, booking_id: bid, doctor: doc.name, specialty: doc.specialty,
        branch, date, time, fee: doc.fee || 0, queue_no: queueNo,
        note: `Appointment confirmed. Read this back to the caller: ${patient} with ${doc.name} (${doc.specialty}) at ${branch} branch on ${date} at ${time}, consultation fee Rs ${doc.fee || 0}. Appointment number ${bid}. Queue number ${queueNo}. A confirmation SMS will follow.`,
      });
      return;
    }

    if (name === "book_reservation") {
      const ref = loadRef("reservations");
      const guest = String(args.guest_name || "").trim();
      const phone = String(args.phone || "").trim();
      const party = Math.max(0, parseInt(String(args.party_size || "0"), 10) || 0);
      const date = String(args.date || "").trim();
      const time = String(args.time || "").trim();
      const notes = String(args.notes || "").trim();
      let area = String(args.area || "").trim();
      let branch = String(args.branch || "").trim();

      if (!validName(guest) || !validPhone(phone) || !party || !date || !time) {
        state.gemini?.sendToolResponse(id, name, {
          ok: false, error: "need_details",
          message: "Do NOT book yet. First ASK the guest for their name, a contact phone number, party size, date and time — use EXACTLY what they say, never guess or invent details. Once you have them, call book_reservation again.",
        });
        return;
      }
      const maxParty = Number(ref.maxPartySize || 20);
      if (party > maxParty) {
        state.gemini?.sendToolResponse(id, name, {
          ok: false, error: "party_too_large",
          message: `Parties over ${maxParty} need a private room or a manager. Offer that instead of booking a normal table.`,
        });
        return;
      }
      const areas: string[] = ref.areas || [];
      if (area && areas.length && !areas.some((a) => a.toLowerCase().includes(area.toLowerCase()) || area.toLowerCase().includes(a.toLowerCase()))) area = "";
      if (!area) area = areas[0] || "Indoor";
      const branches: string[] = ref.branches || [];
      if (branch && branches.length && !branches.some((b) => b.toLowerCase() === branch.toLowerCase())) branch = "";
      if (!branch && branches.length) branch = branches[0];
      const deposit = party * Number(ref.depositPerGuest || 0);
      const bid = makeId("RS");
      const rec = {
        id: bid, ref: bid, guest, phone, party, date, time, area, branch,
        channel: "AI call", status: "confirmed", deposit, paid: false,
        notes, source: "AI call", call_uuid: uuid, created: nowIso(),
      };
      try {
        writeRecord("reservations", "reservations", rec);
      } catch (e) {
        console.error("[bridge] reservation write failed:", (e as Error).message);
        appendSessionEvent(uuid, { type: "booking_failed", error: (e as Error).message });
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "save_failed" });
        return;
      }
      appendSessionEvent(uuid, { type: "booking_created", kind: "reservation", booking_id: bid, guest, party, date, time, area });
      state.gemini?.sendToolResponse(id, name, {
        ok: true, booking_id: bid, guest, party, date, time, area, branch, deposit,
        note: `Reservation confirmed. Read back to the guest: ${guest}, party of ${party}, ${area}${branch ? " at " + branch : ""}, on ${date} at ${time}.` + (deposit ? ` A deposit of Rs ${deposit} applies.` : "") + ` Confirmation reference ${bid}.`,
      });
      return;
    }

    if (name === "find_product") {
      const ref = loadRef("sales");
      const q = String(args.query || "").toLowerCase().trim();
      const reserved = salesReserved();
      let prods = (ref.products || []).map((p: any) => ({
        sku: p.sku, name: p.name, price: p.price, category: p.category,
        available: Math.max(0, Number(p.stock || 0) - (reserved[p.sku] || 0)),
      }));
      if (q) prods = prods.filter((p: any) => (p.name + " " + (p.category || "")).toLowerCase().includes(q));
      prods = prods.slice(0, 5).map((p: any) => ({ sku: p.sku, name: p.name, price: p.price, available: p.available, in_stock: p.available > 0 }));
      appendSessionEvent(uuid, { type: "lookup", kind: "product", query: q, count: prods.length });
      state.gemini?.sendToolResponse(id, name, {
        count: prods.length, products: prods, currency: ref.currency || "Rs",
        note: prods.length === 0
          ? "No matching product in the catalogue. Ask the caller to describe what they want, or offer a popular item."
          : "Live catalogue results with current stock. Tell the caller the price and whether it is in stock before taking the order.",
      });
      return;
    }

    if (name === "place_order") {
      const ref = loadRef("sales");
      const customer = String(args.customer_name || "").trim();
      const phone = String(args.phone || "").trim();
      const address = String(args.address || "").trim();
      const payRaw = String(args.payment || "COD").trim().toLowerCase();
      const qty = Math.max(1, parseInt(String(args.quantity || "1"), 10) || 1);
      const pq = String(args.product || "").toLowerCase().trim();
      if (!pq || !validName(customer) || !validPhone(phone)) {
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "need_details", message: "Do NOT place the order yet. First ASK the customer for their name and a contact phone number, and confirm the product — use EXACTLY what they say, never guess or invent. Then call place_order again." });
        return;
      }
      const prod = (ref.products || []).find((p: any) =>
        String(p.sku).toLowerCase() === pq || String(p.name).toLowerCase().includes(pq));
      if (!prod) {
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "no_product", message: "No such product. Use find_product first, then try again." });
        return;
      }
      const avail = Math.max(0, Number(prod.stock || 0) - (salesReserved()[prod.sku] || 0));
      if (qty > avail) {
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "out_of_stock", available: avail, message: avail ? `Only ${avail} in stock — offer that quantity.` : `${prod.name} is out of stock — offer an alternative.` });
        return;
      }
      const total = prod.price * qty;
      const payment = payRaw === "card" ? "Card" : "COD";
      const bid = makeId("ORD");
      const rec = {
        id: bid, ref: bid, customer, phone, address,
        items: `${qty}× ${prod.name}`, lines: [{ sku: prod.sku, name: prod.name, qty, price: prod.price }],
        qty, total, payment, channel: "AI call", source: "AI call",
        status: "pending", paid: false, call_uuid: uuid, created: nowIso(),
      };
      try {
        writeRecord("sales", "orders", rec);
      } catch (e) {
        appendSessionEvent(uuid, { type: "booking_failed", error: (e as Error).message });
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "save_failed" });
        return;
      }
      appendSessionEvent(uuid, { type: "booking_created", kind: "order", booking_id: bid, customer, product: prod.name, qty, total });
      state.gemini?.sendToolResponse(id, name, {
        ok: true, order_ref: bid, product: prod.name, quantity: qty, total, currency: ref.currency || "Rs", payment,
        note: `Order placed. Read back to the caller: ${qty} × ${prod.name} for ${customer}, total ${ref.currency || "Rs"} ${total}, payment ${payment}. Confirmation reference ${bid}. Delivery will follow and we may call to confirm the address.`,
      });
      return;
    }

    if (name === "confirm_order") {
      const outcome = String(args.outcome || "").toLowerCase().trim();
      const note = String(args.note || "").trim();
      const ref = state.orderRef;
      const vert = state.orderVertical || "sales";
      if (ref) {
        const file = path.join(BOOKINGS_DIR, vert, "orders", `${ref}.json`);
        try {
          const o = JSON.parse(fs.readFileSync(file, "utf-8"));
          if (outcome === "confirmed") o.status = "confirmed";
          else if (outcome === "cancelled") o.status = "cancelled";
          o.confirm_outcome = outcome;
          o.confirmed_at = nowIso();
          o.confirmed_by = "AI outbound";
          if (note) o.confirm_note = note;
          const tmp = file + ".tmp";
          fs.writeFileSync(tmp, JSON.stringify(o, null, 2));
          fs.renameSync(tmp, file);
        } catch (e) {
          console.warn("[bridge] confirm_order update failed:", (e as Error).message);
        }
      }
      appendSessionEvent(uuid, { type: "order_confirm", order_ref: ref, outcome, note });
      state.gemini?.sendToolResponse(id, name, { ok: true, outcome });
      return;
    }

    if (name === "order_lab_test") {
      const t = matchTest(String(args.test || ""));
      const patient = String(args.patient_name || "").trim();
      const phone = String(args.phone || "").trim();
      const pr = String(args.priority || "Routine");
      const priority = /stat/i.test(pr) ? "STAT" : /urgent/i.test(pr) ? "Urgent" : "Routine";
      if (!validName(patient) || !validPhone(phone)) {
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "need_details", message: "Do NOT order yet. First ASK the patient for their full name and a contact phone number — use EXACTLY what they say, never guess or invent. Then call order_lab_test again." });
        return;
      }
      if (!t) {
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "no_test", message: "No matching test. Ask the caller to clarify, or offer common tests like Full Blood Count, Lipid Profile, Fasting Blood Sugar." });
        return;
      }
      const mrn = upsertPatient(patient, phone);
      const bid = makeId("LAB");
      const rec = {
        id: bid, ref: bid, accession: makeId("AC"), patient, phone, mrn,
        test_code: t.code, test_name: t.name, department: t.department, specimen: t.specimen,
        panel: (t.analytes || []).length > 1, priority, cost: t.price || 0,
        status: "ordered", ordered_by: "AI agent", source: "AI call",
        ordered_at: nowIso(), results: [], critical: false, created: nowIso(),
      };
      try {
        writeRecord("hospital", "labs", rec);
      } catch (e) {
        appendSessionEvent(uuid, { type: "lab_order_failed", error: (e as Error).message });
        state.gemini?.sendToolResponse(id, name, { ok: false, error: "save_failed" });
        return;
      }
      appendSessionEvent(uuid, { type: "lab_ordered", ref: bid, test: t.name, patient });
      state.gemini?.sendToolResponse(id, name, {
        ok: true, order_ref: bid, test: t.name, price: t.price, specimen: t.specimen, department: t.department,
        note: `Lab order placed: ${t.name}, sample ${t.specimen}, fee Rs ${t.price || 0}. The patient can visit the lab to give the sample. Reference ${bid}.`,
      });
      return;
    }

    if (name === "call_outcome") {
      const outcome = String(args.outcome || "").toLowerCase().trim();
      const note = String(args.note || "").trim();
      const octx = state.outboundCtx || {};
      const vert = octx.vertical || state.orderVertical || "hospital";
      const coll = octx.collection || "appointments";
      const ref = octx.ref || state.orderRef;
      if (ref) {
        const file = path.join(BOOKINGS_DIR, vert, coll, `${ref}.json`);
        try {
          const o = JSON.parse(fs.readFileSync(file, "utf-8"));
          const kind = octx.kind || "";
          if (kind === "appt_confirm" || kind === "appt_reminder") {
            if (outcome === "confirmed") o.status = "confirmed";
            else if (outcome === "cancelled") o.status = "cancelled";
          } else if (kind === "lab_ready" || kind === "lab_critical") {
            o.status = "delivered"; o.delivered_at = nowIso(); o.delivered_via = "AI call";
            if (kind === "lab_critical") { o.critical_ack = (outcome === "acknowledged" || outcome === "confirmed"); o.critical_ack_at = nowIso(); }
          }
          o.call_outcome = outcome; if (note) o.call_note = note; o.called_at = nowIso(); o.called_by = "AI outbound";
          const tmp = file + ".tmp"; fs.writeFileSync(tmp, JSON.stringify(o, null, 2)); fs.renameSync(tmp, file);
        } catch (e) {
          console.warn("[bridge] call_outcome update failed:", (e as Error).message);
        }
      }
      appendSessionEvent(uuid, { type: "call_outcome", kind: octx.kind, ref, outcome, note });
      state.gemini?.sendToolResponse(id, name, { ok: true, outcome });
      return;
    }

    console.warn("[bridge] unknown tool:", name);
    state.gemini?.sendToolResponse(id, name, {
      ok: false,
      error: "unknown_tool",
    });
  } catch (e) {
    console.error("[bridge] tool handler error:", (e as Error).message);
    appendSessionEvent(uuid, {
      type: "tool_handler_error",
      error: (e as Error).message,
    });
  }
}

function close(state: BridgeState, reason: string) {
  if (state.closed) return;
  state.closed = true;
  console.log(`[bridge] closing (${reason})`);
  if (state.paceTimer) {
    clearInterval(state.paceTimer);
    state.paceTimer = null;
  }
  if (state.gemini) state.gemini.close();
  try {
    state.sock.end();
  } catch (_) {}

  // After we close, remove channel registry file (free disk)
  if (state.callerUuid) {
    const formatted =
      state.callerUuid.length === 32
        ? `${state.callerUuid.slice(0, 8)}-${state.callerUuid.slice(
            8,
            12
          )}-${state.callerUuid.slice(12, 16)}-${state.callerUuid.slice(
            16,
            20
          )}-${state.callerUuid.slice(20)}`
        : state.callerUuid;
    appendSessionEvent(formatted, { type: "session_close", reason });
    try {
      fs.unlinkSync(path.join(CHANNEL_REGISTRY_DIR, `${formatted}.chan`));
    } catch (_) {}
    try {
      fs.unlinkSync(path.join(OUTBOUND_DIR, `${formatted}.json`));
    } catch (_) {}
  }
}

// ============================================================
// Servers — primary + retry
// ============================================================

// Bootstrap: pull the sampath.lk live data cache BEFORE accepting any calls,
// so that tool calls during the very first session have something to search.
// On failure we still come up (the cache will be empty and tools will return
// 0 matches — Samali falls back to manually-known info per the system prompt).
async function bootstrap() {
  console.log("[bridge] loading sampath.lk live data cache ...");
  const t0 = Date.now();
  await refreshSampathData();
  const stats = getCacheStats();
  console.log(
    `[bridge] cache loaded in ${Date.now() - t0}ms — ${stats.branches} branches, ${stats.rates} rates`
  );
  startBackgroundRefresh();

  const serverMain = net.createServer(makeHandler(false));
  serverMain.listen(LISTEN_PORT_MAIN, LISTEN_HOST, () => {
    console.log(
      `[bridge] primary AudioSocket on ${LISTEN_HOST}:${LISTEN_PORT_MAIN}`
    );
  });
  const serverRetry = net.createServer(makeHandler(true));
  serverRetry.listen(LISTEN_PORT_RETRY, LISTEN_HOST, () => {
    console.log(
      `[bridge] retry AudioSocket on ${LISTEN_HOST}:${LISTEN_PORT_RETRY}`
    );
  });

  const serverOutbound = net.createServer(makeHandler(false, true));
  serverOutbound.listen(LISTEN_PORT_OUTBOUND, LISTEN_HOST, () => {
    console.log(
      `[bridge] outbound AudioSocket on ${LISTEN_HOST}:${LISTEN_PORT_OUTBOUND}`
    );
  });

  // Event-loop watchdog: if Node.js stalls (sync work, GC, hung await), AudioSocket
  // frames stop flowing within ~2s and Asterisk's app_audiosocket times out at 2000ms.
  // Sample every 5s; warn when p99 stall exceeds 200ms.
  const elDelay = monitorEventLoopDelay({ resolution: 20 });
  elDelay.enable();
  setInterval(() => {
    const p99 = elDelay.percentile(99) / 1e6;
    const max = elDelay.max / 1e6;
    const mean = elDelay.mean / 1e6;
    if (p99 > 200) {
      console.warn(
        `[bridge] event-loop stall: p99=${p99.toFixed(0)}ms max=${max.toFixed(0)}ms mean=${mean.toFixed(0)}ms`
      );
    }
    elDelay.reset();
  }, 5000).unref();

  process.on("SIGTERM", () => {
    console.log("[bridge] SIGTERM");
    serverMain.close();
    serverOutbound.close();
    serverRetry.close(() => process.exit(0));
  });
  process.on("SIGINT", () => {
    console.log("[bridge] SIGINT");
    serverMain.close();
    serverOutbound.close();
    serverRetry.close(() => process.exit(0));
  });
}

bootstrap().catch((e) => {
  console.error("[bridge] bootstrap failed:", e);
  process.exit(1);
});

