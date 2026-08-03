import fs from "fs";
import path from "path";

// ============================================================
// FlowConfig — superset of the legacy AgentConfig.
// Adds: id, name, description, transfer_rules, tools_enabled,
// tools_config, flow (visual graph). All legacy fields preserved
// so callers (gemini-live.ts, bridge.ts) keep working unchanged.
// ============================================================

export interface TransferRule {
  category: string;
  manager_number: string;
  description?: string;
}

export interface FlowGraph {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  viewport?: { x: number; y: number; zoom: number };
}

export interface FlowConfig {
  // Identity
  id?: string;
  name?: string;
  description?: string;
  is_preset?: boolean;

  // Legacy AgentConfig fields (kept for back-compat)
  model: string;
  voice: string;
  greeting_trigger: string;
  retry_greeting_trigger: string;
  manager_number: string;           // legacy single number (now a fallback)
  test_mode?: boolean;
  test_mode_number?: string;
  escalation_timeout_sec: number;
  hold_music_class: string;
  escalation_announcement: string;
  system_prompt: string;
  custom_instructions?: string;
  voices: string[];
  language_hint?: string;

  // New multi-agent additions
  transfer_rules?: TransferRule[];
  tools_enabled?: string[];
  tools_config?: Record<string, Record<string, unknown>>;
  flow?: FlowGraph;

  // v2 additions
  working_hours?: WorkingHoursConfig;     // out-of-hours behavior
  record_calls?: boolean;                  // hint to dialplan via channel var
}

export interface WorkingHoursConfig {
  enabled: boolean;
  timezone: string;                        // IANA, e.g. "Asia/Colombo"
  // Days are 0=Sunday..6=Saturday. Each entry: HH:MM-HH:MM 24h. Empty = closed that day.
  schedule: Record<string, string>;        // "0": "", "1": "09:00-17:00", ...
  out_of_hours_action: "greet" | "transfer" | "hangup";
  out_of_hours_greeting?: string;          // for "greet"
  out_of_hours_transfer_category?: string; // for "transfer" — picks from transfer_rules
  out_of_hours_hangup_message?: string;    // for "hangup"
}

// Re-export legacy alias so existing imports (`AgentConfig`) keep working.
export type AgentConfig = FlowConfig;

// ============================================================
// Paths
// ============================================================

const LEGACY_CONFIG_PATH =
  process.env.AGENT_CONFIG_PATH ||
  path.join(process.cwd(), "agent-config.json");

const FLOWS_DIR =
  process.env.FLOWS_DIR || "/var/lib/sampath-ai/flows";

const ACTIVE_POINTER =
  process.env.ACTIVE_FLOW_FILE || "/var/lib/sampath-ai/active-flow.json";

// ============================================================
// Defaults
// ============================================================

const DEFAULT: FlowConfig = {
  model: "gemini-3.1-flash-live-preview",
  voice: "Aoede",
  greeting_trigger:
    "The customer has just connected to the call. Please greet them now.",
  retry_greeting_trigger:
    "The customer was just brought back to you because the manager was unavailable. Apologise warmly and offer to help instead.",
  manager_number: "0779190005",
  test_mode: true,
  test_mode_number: "0779190005",
  escalation_timeout_sec: 60,
  hold_music_class: "default",
  escalation_announcement:
    "Please hold on a moment while I connect you to our support team.",
  system_prompt: "You are a helpful voice assistant.",
  voices: [
    "Zephyr", "Kore", "Aoede", "Leda", "Callirrhoe", "Autonoe", "Despina",
    "Erinome", "Laomedeia", "Achernar", "Gacrux", "Pulcherrima",
    "Vindemiatrix", "Sulafat",
  ],
  transfer_rules: [
    { category: "default", manager_number: "0779190005", description: "Default escalation target." },
  ],
  tools_enabled: ["save_customer_info", "request_human_transfer", "end_call"],
  tools_config: {},
};

// ============================================================
// Loader: read active-flow.json -> flows/<id>.json with fallback
// ============================================================

function readJson<T>(p: string): T | null {
  try {
    return JSON.parse(fs.readFileSync(p, "utf-8")) as T;
  } catch (e) {
    return null;
  }
}

function readLegacy(): FlowConfig | null {
  try {
    const raw = fs.readFileSync(LEGACY_CONFIG_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    return { ...DEFAULT, ...parsed };
  } catch (_) {
    return null;
  }
}

export function loadAgentConfig(): FlowConfig {
  // 1) Try active flow
  const pointer = readJson<{ active_id?: string }>(ACTIVE_POINTER);
  if (pointer && pointer.active_id) {
    const flowPath = path.join(FLOWS_DIR, `${pointer.active_id}.json`);
    const flow = readJson<FlowConfig>(flowPath);
    if (flow) {
      // Defaults filled in for any field a flow JSON omits.
      // Manager_number legacy field gets seeded from default transfer_rule
      // so any old code path that reads cfg.manager_number still works.
      const merged: FlowConfig = { ...DEFAULT, ...flow };
      if ((!flow.manager_number || flow.manager_number === DEFAULT.manager_number) &&
          flow.transfer_rules && flow.transfer_rules.length) {
        const def = flow.transfer_rules.find((r) => r.category === "default") || flow.transfer_rules[0];
        merged.manager_number = def.manager_number;
      }
      return merged;
    }
    console.warn(
      `[flows] active flow '${pointer.active_id}' not loadable from ${flowPath}, falling back to legacy config`
    );
  } else {
    console.warn(`[flows] no active-flow.json pointer, falling back to legacy config`);
  }

  // 2) Fallback: legacy single-config file
  const legacy = readLegacy();
  if (legacy) return legacy;

  // 3) Last resort: built-in defaults
  console.warn(`[flows] legacy config also unreadable, using built-in defaults`);
  return DEFAULT;
}

// ============================================================
// System-prompt builder (unchanged behaviour)
// ============================================================

function getSriLankaParts(): {
  hour: number; minute: number; weekday: string; date: string; clock: string;
} {
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Colombo",
    hour: "2-digit", minute: "2-digit", weekday: "long",
    day: "2-digit", month: "long", year: "numeric", hour12: false,
  });
  const parts: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date())) {
    if (p.type !== "literal") parts[p.type] = p.value;
  }
  let hour = parseInt(parts.hour || "0", 10);
  if (hour === 24) hour = 0;
  return {
    hour,
    minute: parseInt(parts.minute || "0", 10),
    weekday: parts.weekday || "",
    date: `${parts.day || ""} ${parts.month || ""} ${parts.year || ""}`,
    clock: `${String(hour).padStart(2, "0")}:${parts.minute || "00"}`,
  };
}

function getTimeGreetingContext(): string {
  const t = getSriLankaParts();
  let band: string;
  if (t.hour >= 5 && t.hour < 12) band = "MORNING";
  else if (t.hour >= 12 && t.hour < 17) band = "AFTERNOON";
  else if (t.hour >= 17 && t.hour < 20) band = "EVENING";
  else band = "NIGHT";

  const greetings: Record<string, string> = {
    MORNING: "Sinhala: 'සුබ උදෑසනක්' | English: 'Good morning' | Tamil: 'காலை வணக்கம்'",
    AFTERNOON: "Sinhala: 'සුබ දහවලක්' | English: 'Good afternoon' | Tamil: 'மதிய வணக்கம்'",
    EVENING: "Sinhala: 'සුබ සැන්දෑවක්' | English: 'Good evening' | Tamil: 'மாலை வணக்கம்'",
    NIGHT: "Sinhala: 'සුබ රාත්‍රියක්' | English: 'Good evening' | Tamil: 'இரவு வணக்கம்'",
  };

  return [
    `## CURRENT TIME IN SRI LANKA (Asia/Colombo, UTC+5:30)`,
    `It is ${t.weekday}, ${t.date}, ${t.clock} local time in Sri Lanka.`,
    `Time-of-day band: **${band}** — greet with: ${greetings[band]}.`,
    `Greet using the opening language your own instructions specify — on an inbound call the caller has not spoken yet, so follow your opening rule and do NOT try to infer their language from the greeting. Switch language only AFTER the caller replies, per your LANGUAGE rule. Use the time band above — do not greet in a different time band.`,
  ].join("\n");
}

export function buildSystemText(cfg: FlowConfig, callerMemory?: string): string {
  let txt = cfg.system_prompt || "";
  const custom = (cfg.custom_instructions || "").trim();
  if (custom) {
    txt += "\n\n---\n\n## CUSTOM INSTRUCTIONS (admin-editable)\n\n" + custom;
  }
  txt += "\n\n" + getTimeGreetingContext();
  if (callerMemory) txt += "\n\n" + callerMemory;
  return txt;
}
