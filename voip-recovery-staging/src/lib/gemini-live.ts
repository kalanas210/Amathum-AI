import WebSocket from "ws";
import { buildMemoryContext, loadMemory } from "./memory";
import { AgentConfig, FlowConfig, buildSystemText } from "./agent-config";

function getWsUrl(): string {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) throw new Error("GEMINI_API_KEY not set");
  return `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${apiKey}`;
}

export interface ToolCallEvent {
  name: string;
  args: Record<string, unknown>;
  id: string;
}

export interface GeminiLiveSession {
  ws: WebSocket;
  send: (audioBase64: string) => void;
  sendText: (text: string) => void;
  sendToolResponse: (id: string, name: string, result: unknown) => void;
  close: () => void;
  onAudio: (cb: (audioBase64: string) => void) => void;
  onTranscript: (cb: (text: string, role: string) => void) => void;
  onInterrupt: (cb: () => void) => void;
  onTurnComplete: (cb: () => void) => void;
  onToolCall: (cb: (call: ToolCallEvent) => void) => void;
  onError: (cb: (error: string) => void) => void;
  onClose: (cb: () => void) => void;
}

// ============================================================
// Tool Registry — single source of truth for available tools.
// A flow's tools_enabled[] selects which of these the Gemini model
// sees on each call. Runtime handlers for each tool live in bridge.ts
// (handleToolCall) — this registry only declares the SCHEMA Gemini sees.
// ============================================================

interface ToolDef {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  // Optional: enrich the description with per-flow config (e.g. list of
  // allowed save_customer_info fields, list of available transfer categories).
  enrich?: (cfg: FlowConfig) => { description?: string; parameters?: Record<string, unknown> };
}

const TOOL_REGISTRY: Record<string, ToolDef> = {
  find_sampath_branch: {
    name: "find_sampath_branch",
    description:
      "Look up live Sampath Bank branch details (address, phone, manager, email, province) from the bank's own database. Use this whenever the customer asks about a SPECIFIC branch, ATM, or area — e.g. 'Sampath Kirulapone branch contact', 'nearest branch to Kandy', 'Kotahena branch address', 'Gampaha branch phone number'. Returns up to 4 matches ranked by relevance. Before calling say a short filler like 'ටිකක් check කරන්නම්' / 'Let me check that' so the customer doesn't think the line went silent.",
    parameters: {
      type: "OBJECT",
      properties: {
        query: {
          type: "STRING",
          description: "The location, area, town, or branch name the customer mentioned, e.g. 'Kirulapone', 'Kandy', 'Wellawatte', 'Battaramulla'. Use the exact word the customer said.",
        },
      },
      required: ["query"],
    },
  },

  get_exchange_rates: {
    name: "get_exchange_rates",
    description:
      "Get live foreign-exchange rates from Sampath Bank (updated daily). Use when the customer asks about a currency rate, e.g. 'today's USD rate', 'euro to LKR', 'how much for British pounds?'. If they ask 'today's rates' generally, pass no currency and you'll get all of them. Always read the effective_from timestamp aloud so the customer knows when it was set.",
    parameters: {
      type: "OBJECT",
      properties: {
        currency: {
          type: "STRING",
          description: "Currency code (USD, EUR, GBP, JPY, AUD, etc.) OR a partial name ('dollar', 'pound', 'euro'). Leave empty/omit for all currencies.",
        },
      },
    },
  },

  save_customer_info: {
    name: "save_customer_info",
    description:
      "Save a piece of information you learned about the customer. Call EVERY TIME the customer tells you something concrete (their name, NIC, account number, phone, the nature of their problem, etc.). Call silently in the background while you keep talking — do NOT announce to the customer that you are saving.",
    parameters: {
      type: "OBJECT",
      properties: {
        field: {
          type: "STRING",
          description: "Short snake_case key for the info, e.g. 'name', 'nic', 'account_number', 'phone', 'complaint', 'language', 'preferred_branch'",
        },
        value: {
          type: "STRING",
          description: "The value the customer told you, verbatim",
        },
      },
      required: ["field", "value"],
    },
    enrich(cfg) {
      const fields = (cfg.tools_config?.save_customer_info as any)?.fields as string[] | undefined;
      if (!fields || !fields.length) return {};
      return {
        description:
          this.description +
          ` Recommended fields for this flow: ${fields.join(", ")}. Stick to these keys when possible.`,
      };
    },
  },

  find_doctor: {
    name: "find_doctor",
    description:
      "Look up which doctors are available, from the hospital's directory. Use this whenever the caller describes a symptom or asks for a specialty / doctor / branch — e.g. 'a doctor for hair loss', 'skin doctor in Matara', 'child doctor', 'cardiologist in Colombo'. You may pass a symptom, a specialty, a branch and/or a doctor name — any combination. Before calling, say a short filler like 'ටිකක් බලන්නම්' / 'Let me check who is available'. Returns matching doctors with branch, consultation fee and available times. Offer one or two options to the caller and let them choose BEFORE you call book_appointment.",
    parameters: {
      type: "OBJECT",
      properties: {
        symptom: { type: "STRING", description: "What the patient needs help with, in their words, e.g. 'hair loss', 'chest pain', 'toothache', 'eye checkup'. Optional." },
        specialty: { type: "STRING", description: "Specialty name if the caller named one, e.g. 'Dermatology', 'Cardiology', 'Dental'. Optional." },
        branch: { type: "STRING", description: "Preferred branch / city if mentioned, e.g. 'Matara', 'Colombo', 'Kandy'. Optional." },
        doctor: { type: "STRING", description: "A specific doctor's name if the caller asked for one. Optional." },
      },
    },
  },

  book_appointment: {
    name: "book_appointment",
    description:
      "Create a confirmed appointment booking. NEVER invent, guess or assume the patient's name or phone number — if the caller has not actually told you, ask them first and do NOT call this tool yet. Only call this AFTER you have collected and read back ALL of: patient full name, contact phone number, the doctor (or at least the specialty), the branch, and the date and time — and the caller has confirmed they are correct. Use find_doctor first to pick a real doctor / branch / time. If it returns ok:true, tell the caller the appointment is confirmed and read out the confirmation reference (appointment number), the queue number (queue_no) and the consultation fee — all exactly as returned. If it returns ok:false, apologise and offer an alternative based on the error.",
    parameters: {
      type: "OBJECT",
      properties: {
        patient_name: { type: "STRING", description: "The patient's full name, verbatim." },
        phone: { type: "STRING", description: "Contact phone number, exactly as the caller gave or confirmed it. If a CALLER'S PHONE NUMBER is provided in your context (from caller ID) and the caller confirms it, use that. NEVER invent or guess a number — if you do not have one, ask the caller." },
        doctor: { type: "STRING", description: "Chosen doctor's name from find_doctor, e.g. 'Dr. Nimal Sooriya'. Provide this OR specialty." },
        specialty: { type: "STRING", description: "Medical specialty if a specific doctor wasn't chosen, e.g. 'Dermatology'." },
        branch: { type: "STRING", description: "The branch / city for the visit, e.g. 'Matara'." },
        date: { type: "STRING", description: "Appointment date as YYYY-MM-DD. Resolve relative dates ('tomorrow', 'next Monday') to an absolute date using the current Sri Lanka date given in your context." },
        time: { type: "STRING", description: "Appointment time in 24-hour HH:MM, e.g. '13:00' for 1 pm." },
        reason: { type: "STRING", description: "Short reason / symptom for the visit, e.g. 'hair loss'. Optional." },
      },
      required: ["patient_name", "date", "time"],
    },
  },

  book_reservation: {
    name: "book_reservation",
    description:
      "Create a confirmed table reservation. NEVER invent or guess the guest's name or phone number — if the caller hasn't given them, ask first and do NOT call this tool yet. Only proceed once you have collected and read back ALL of: guest name, contact phone, party size (number of people), date and time, and the seating area if the guest asked for one. Only call this AFTER the guest confirms the details. After ok:true, tell the guest the reservation is confirmed, read out the confirmation reference, and mention the deposit if one applies. If ok:false, apologise and offer an alternative (a different time, or a private room / smaller party for large groups).",
    parameters: {
      type: "OBJECT",
      properties: {
        guest_name: { type: "STRING", description: "The guest's full name, verbatim." },
        phone: { type: "STRING", description: "Contact phone number, exactly as the guest gave or confirmed it. If a CALLER'S PHONE NUMBER is provided in your context (from caller ID) and the guest confirms it, use that. NEVER invent or guess a number — if you do not have one, ask." },
        party_size: { type: "NUMBER", description: "Number of people / covers, e.g. 4." },
        date: { type: "STRING", description: "Reservation date as YYYY-MM-DD. Resolve 'today / tomorrow / this Friday' to an absolute date using the current Sri Lanka date in your context." },
        time: { type: "STRING", description: "Reservation time in 24-hour HH:MM, e.g. '19:30'." },
        area: { type: "STRING", description: "Preferred seating area if mentioned, e.g. 'Garden', 'Rooftop', 'Indoor', 'Private Room'. Optional." },
        branch: { type: "STRING", description: "Branch / outlet if there is more than one and the guest named it. Optional." },
        notes: { type: "STRING", description: "Any special request — birthday, allergy, high chair, window seat, etc. Optional." },
      },
      required: ["guest_name", "party_size", "date", "time"],
    },
  },

  find_product: {
    name: "find_product",
    description:
      "Search the store's product catalogue and check LIVE stock. Use this whenever the caller asks about a product, a category, or what's available — e.g. 'do you have wireless earbuds?', 'show me kitchen items', 'is the air fryer in stock?'. Before calling say a short filler like 'ටිකක් බලන්නම්' / 'Let me check'. Returns matching products with price and current availability. Tell the caller the price and whether it's in stock before taking an order.",
    parameters: {
      type: "OBJECT",
      properties: {
        query: { type: "STRING", description: "The product name, category or keyword the caller mentioned, e.g. 'earbuds', 'air fryer', 'kitchen', 'watch'. Leave empty to list everything." },
      },
    },
  },

  place_order: {
    name: "place_order",
    description:
      "Place a product order. NEVER invent or guess the customer's name or phone number — ask the caller first if you don't have them, and do NOT call this tool yet. Only proceed once you have confirmed the product, quantity, the customer's name and phone, and (ideally) a delivery address. Use find_product FIRST to confirm the item is in stock. Only call this after the caller confirms. After ok:true, tell the caller the order is placed, read out the confirmation reference and total, and that delivery will follow (cash on delivery by default). If ok:false, explain (out of stock / unknown product) and offer an alternative.",
    parameters: {
      type: "OBJECT",
      properties: {
        product: { type: "STRING", description: "The product name (or SKU) the caller wants, e.g. 'Wireless Earbuds Pro'." },
        quantity: { type: "NUMBER", description: "How many they want. Default 1." },
        customer_name: { type: "STRING", description: "The customer's full name." },
        phone: { type: "STRING", description: "Contact phone number, exactly as the customer gave or confirmed it. If a CALLER'S PHONE NUMBER is provided in your context (from caller ID) and the customer confirms it, use that. NEVER invent or guess a number — if you do not have one, ask." },
        address: { type: "STRING", description: "Delivery address. Optional but ask for it." },
        payment: { type: "STRING", description: "'COD' (cash on delivery, default) or 'Card'." },
      },
      required: ["product", "customer_name", "phone"],
    },
  },

  order_lab_test: {
    name: "order_lab_test",
    description:
      "Order a laboratory test or panel for a patient. Use when the caller asks for a blood test, scan or lab investigation — e.g. 'I need a full blood count', 'book a lipid profile', 'dengue test', 'HbA1c'. ALWAYS ask the caller for the patient's real name and phone number first — never invent, guess or assume them. After ok:true, tell them the test name, the sample required, the fee, and that they can come to the lab to give the sample; read out the order reference. If ok:false, ask them to clarify or offer common tests.",
    parameters: {
      type: "OBJECT",
      properties: {
        test: { type: "STRING", description: "The test or panel the caller wants, e.g. 'full blood count', 'lipid profile', 'fasting blood sugar', 'dengue', 'HbA1c', 'thyroid'." },
        patient_name: { type: "STRING", description: "The patient's full name." },
        phone: { type: "STRING", description: "Contact phone number, exactly as the caller gave or confirmed it. If a CALLER'S PHONE NUMBER is provided in your context (from caller ID) and the caller confirms it, use that. NEVER invent or guess a number — if you do not have one, ask the caller." },
        priority: { type: "STRING", description: "'Routine' (default), 'Urgent', or 'STAT'." },
      },
      required: ["test", "patient_name", "phone"],
    },
  },

  call_outcome: {
    name: "call_outcome",
    description:
      "Record the outcome of an OUTBOUND hospital call (appointment reminder/confirmation, a 'lab results ready' call, or a critical-result callback). Call this once the patient has responded. outcome: 'confirmed' (they confirm / heard you), 'cancelled' (cancel the appointment), 'reschedule' (wants a different time — detail in note), or 'acknowledged' (understood a critical result). After calling this, thank the patient and call end_call.",
    parameters: {
      type: "OBJECT",
      properties: {
        outcome: { type: "STRING", description: "'confirmed', 'cancelled', 'reschedule', or 'acknowledged'." },
        note: { type: "STRING", description: "Any detail — a requested time, or what the patient said. Optional." },
      },
      required: ["outcome"],
    },
  },

  confirm_order: {
    name: "confirm_order",
    description:
      "Record the outcome of an OUTBOUND order-confirmation call. Call this once the customer has said whether the order is correct: outcome 'confirmed' if they confirm it, 'cancelled' if they want to cancel, or 'reschedule' if they want changes or a callback (put details in note). After calling this, thank the customer and call end_call.",
    parameters: {
      type: "OBJECT",
      properties: {
        outcome: { type: "STRING", description: "'confirmed', 'cancelled', or 'reschedule'." },
        note: { type: "STRING", description: "Any detail — a requested change, a callback time, or why they cancelled. Optional." },
      },
      required: ["outcome"],
    },
  },

  // ---------------------------------------------------------------------
  // RESTAURANT (QSR phone ordering). Every one of these is proxied by
  // bridge.ts to POST /api/agent/restaurant/<tool> on the Flask monitor, which
  // owns the menu, the promotion engine and the order lifecycle. The agent
  // therefore never computes a price, a total or an order number itself.
  // ---------------------------------------------------------------------

  lookup_customer: {
    name: "lookup_customer",
    description:
      "Look the caller up in the restaurant's customer database. Call this ONCE, silently, at the very start of every call — before you greet them — so you know whether they are a returning customer. If it returns known:true, greet them BY NAME and confirm their saved delivery address instead of asking for it. If known:false, treat them as new and collect name and address during the order. It takes no arguments: it always uses the number this call is coming from, so it cannot be used to look up anybody else. Never tell the caller you are 'looking them up'.",
    parameters: { type: "OBJECT", properties: {} },
  },

  get_menu: {
    name: "get_menu",
    description:
      "Search the live menu and get real prices. Use this EVERY time the caller asks what you have, asks a price, or names something vaguely ('spicy rice', 'what burgers do you have', 'chicken bucket'). Pass their own words as `query`, or a category name as `category`. Returns matching items with the exact price to quote. NEVER speak a price or an item name that did not come back from this tool — if it returns nothing, say you will check with the branch. Read the results out one per line as '<item> — LKR <amount>', then ask which one they would like.",
    parameters: {
      type: "OBJECT",
      properties: {
        query: { type: "STRING", description: "The caller's own words, e.g. 'spicy rice', 'chicken bucket', 'what burgers do you have'. Optional." },
        category: { type: "STRING", description: "A menu category if the caller named one: Burgers, Chicken, Rice Meals, Biriyani, Wraps, Family Meals, Drinks. Optional." },
        limit: { type: "NUMBER", description: "Maximum items to return. Default 12." },
      },
    },
  },

  get_promotions: {
    name: "get_promotions",
    description:
      "Get today's live offers. Use whenever the caller asks about promotions, offers, deals or discounts. Returns only promotions valid for today's date. Read them out one per line and ask if they would like to order any. If the caller picks one, pass that promotion's `id` as `promo_id` to place_food_order — the discount is calculated server-side. NEVER invent, extend or estimate an offer.",
    parameters: { type: "OBJECT", properties: {} },
  },

  get_branch_info: {
    name: "get_branch_info",
    description:
      "Get branch and general shop information: which branches exist, a branch's address and phone, today's opening hours, whether we are OPEN RIGHT NOW, the delivery areas we cover, and accepted payment methods. Use for 'where are you located', 'are you open now', 'what areas do you deliver to', 'how can I pay'. The open_now field is computed against real Sri Lanka time — trust it rather than guessing from the hours.",
    parameters: {
      type: "OBJECT",
      properties: {
        branch: { type: "STRING", description: "Branch name if the caller named one: Colombo, Kandy, Galle or Negombo. Leave empty to list all branches." },
        topic: { type: "STRING", description: "What they asked about: 'location', 'hours', 'contact', 'delivery_areas' or 'payment'. Optional." },
      },
    },
  },

  place_food_order: {
    name: "place_food_order",
    description:
      "Place the food order. Call this ONLY after you have read the WHOLE order back to the caller — every item, the total, the delivery address or pickup branch — and they have explicitly said yes. NEVER invent or guess the caller's name, phone number or address; if you do not have one, ask for it and do NOT call this tool yet. The server recomputes every price, applies any promotion and issues the order number. After ok:true, tell the caller the order is placed, give the estimated time and read out the order number EXACTLY as returned. If ok:false, read the message field — it tells you what to fix (unknown item, missing address, outside our delivery area, or we are closed) — and never place the order anyway.",
    parameters: {
      type: "OBJECT",
      properties: {
        items: {
          type: "ARRAY",
          description: "Everything the caller ordered. One entry per distinct menu item.",
          items: {
            type: "OBJECT",
            properties: {
              item: { type: "STRING", description: "The menu item name exactly as get_menu returned it, e.g. '12 Pieces Bucket', 'Large Pepsi'." },
              quantity: { type: "NUMBER", description: "How many of this item. Default 1." },
            },
            required: ["item"],
          },
        },
        customer_name: { type: "STRING", description: "The customer's name, verbatim. For a returning caller use the name lookup_customer returned — do not ask again." },
        phone: { type: "STRING", description: "Contact phone number, exactly as given or confirmed. Use the caller ID number from your context when the caller confirms it. NEVER invent a number." },
        fulfilment: { type: "STRING", description: "'delivery' (default) or 'pickup'. Use 'pickup' when the caller wants to collect from a branch — then no address is needed." },
        address: { type: "STRING", description: "Full delivery address. Required for delivery. For a returning caller use the saved address they confirmed." },
        branch: { type: "STRING", description: "Branch to collect from — required for pickup: Colombo, Kandy, Galle or Negombo." },
        promo_id: { type: "STRING", description: "The `id` of a promotion from get_promotions, when the caller asked for that offer. Optional." },
        payment: { type: "STRING", description: "Payment method the caller chose. Defaults to cash on delivery." },
        notes: { type: "STRING", description: "Any special request — extra spicy, no onions, call on arrival, etc. Optional." },
      },
      required: ["items", "customer_name", "phone"],
    },
  },

  check_order_status: {
    name: "check_order_status",
    description:
      "Look up an existing order and where it has got to. Use when the caller asks about an order they already placed. Ask them for the order number OR their phone number first — with a phone number you get their most recent open order. If the order is still in the kitchen you get a ready time and a delivery window, and you should offer to have the restaurant prioritise it. If it is already out for delivery you get the rider's name, the rider's phone and an arrival estimate — read the rider's number one digit at a time. Never invent a status, a rider or a time.",
    parameters: {
      type: "OBJECT",
      properties: {
        order_number: { type: "STRING", description: "The order number the caller gives, e.g. '54873' or '#54873'. Optional if you have their phone number." },
        phone: { type: "STRING", description: "The phone number they ordered with. Optional if you have the order number." },
      },
    },
  },

  cancel_order: {
    name: "cancel_order",
    description:
      "Cancel an order. ALWAYS call this twice. First WITHOUT confirm to check whether it can still be cancelled — orders that have already left the kitchen cannot be, and you must then offer to connect the branch instead. Tell the caller what it says and ask them to confirm. Only when they say yes, call it again with confirm set to true. Never tell a caller an order is cancelled before the tool has returned cancelled:true.",
    parameters: {
      type: "OBJECT",
      properties: {
        order_number: { type: "STRING", description: "The order number, e.g. '54873'. Optional if you have their phone number." },
        phone: { type: "STRING", description: "The phone number they ordered with. Optional if you have the order number." },
        confirm: { type: "BOOLEAN", description: "Leave false/absent on the first call. Set true only after the caller has explicitly confirmed they want it cancelled." },
      },
    },
  },

  modify_order: {
    name: "modify_order",
    description:
      "Change an order that is already placed — add items, remove items, change the delivery address, or flag it for the kitchen to prioritise. Only works while the order is still in the kitchen. The server recomputes the total from the real line items and returns it; read that new total back to the caller and ask them to confirm the updated order. NEVER add up a new total yourself. Set prioritise to true when the caller has agreed to you asking the restaurant to hurry their order along.",
    parameters: {
      type: "OBJECT",
      properties: {
        order_number: { type: "STRING", description: "The order number. Optional if you have their phone number." },
        phone: { type: "STRING", description: "The phone number they ordered with. Optional if you have the order number." },
        add_items: {
          type: "ARRAY",
          description: "Items to ADD to the order.",
          items: {
            type: "OBJECT",
            properties: {
              item: { type: "STRING", description: "Menu item name exactly as get_menu returned it." },
              quantity: { type: "NUMBER", description: "How many. Default 1." },
            },
            required: ["item"],
          },
        },
        remove_items: {
          type: "ARRAY",
          description: "Items to REMOVE from the order.",
          items: {
            type: "OBJECT",
            properties: {
              item: { type: "STRING", description: "Menu item name exactly as it appears on the order." },
              quantity: { type: "NUMBER", description: "How many to remove. Default 1." },
            },
            required: ["item"],
          },
        },
        address: { type: "STRING", description: "A new delivery address, if the caller wants it delivered somewhere else. Optional." },
        prioritise: { type: "BOOLEAN", description: "Set true to flag the order for the kitchen to prioritise — only after the caller has agreed. Optional." },
      },
    },
  },

  request_human_transfer: {
    name: "request_human_transfer",
    description:
      "Call this when the customer asks to speak to a real human/manager/supervisor, OR when they are clearly frustrated and your help is not landing, OR when the request requires real system access you cannot provide. BEFORE calling, say a short reassurance to the caller like 'හරි, මම ඔයාව support team එකට connect කරන්නම්, ටිකක් hold කරන්න'.",
    parameters: {
      type: "OBJECT",
      properties: {
        reason: {
          type: "STRING",
          description: "One-sentence reason for transferring, used for logs and to brief the human agent",
        },
        category: {
          type: "STRING",
          description: "Pick the category that best matches the caller's issue. The bridge uses this to route to the correct manager number.",
        },
      },
      required: ["reason"],
    },
    enrich(cfg) {
      const rules = cfg.transfer_rules || [];
      if (rules.length <= 1) {
        return {}; // single default category — no need to enumerate
      }
      const list = rules
        .map((r) => `'${r.category}' (${r.description || "no description"})`)
        .join(", ");
      return {
        description:
          this.description +
          ` Available categories for THIS flow: ${list}. Always pick one; use 'default' if nothing else fits.`,
      };
    },
  },

  end_call: {
    name: "end_call",
    description:
      "Call this when the customer has confirmed they need nothing else and the conversation is wrapping up, OR when they explicitly ask to hang up / end the call. Say a polite closing FIRST ('ස්තූතියි call කළාට, සුභ දවසක්'), then call this tool to hang up.",
    parameters: {
      type: "OBJECT",
      properties: {
        reason: {
          type: "STRING",
          description: "Brief reason for ending, e.g. 'customer_finished', 'customer_requested_hangup'",
        },
      },
      required: ["reason"],
    },
  },
};

export function getToolCatalog(): Array<{ name: string; description: string; parameters: unknown }> {
  return Object.values(TOOL_REGISTRY).map((t) => ({
    name: t.name,
    description: t.description,
    parameters: t.parameters,
  }));
}

function buildTools(cfg: FlowConfig) {
  const enabled = cfg.tools_enabled && cfg.tools_enabled.length
    ? cfg.tools_enabled
    : Object.keys(TOOL_REGISTRY);
  const declarations = enabled
    .map((id) => TOOL_REGISTRY[id])
    .filter((d): d is ToolDef => !!d)
    .map((d) => {
      const enriched = d.enrich ? d.enrich(cfg) : {};
      return {
        name: d.name,
        description: enriched.description || d.description,
        parameters: enriched.parameters || d.parameters,
      };
    });
  return [{ functionDeclarations: declarations }];
}

// ============================================================
// Gemini Live session (unchanged below this line)
// ============================================================

// Context block telling the agent the caller's own number (from caller ID) so it
// offers that for the booking instead of making the caller dictate digits — the
// main source of mis-saved numbers. Empty for withheld/unknown caller IDs.
function callerNumberContext(num?: string): string {
  const n = (num || "").trim();
  if (!n) return "";
  const spaced = n.split("").join(" ");
  return [
    "## CALLER'S PHONE NUMBER (from caller ID — already known)",
    `This inbound call is coming from this phone number: ${n} (spoken digit by digit: ${spaced}).`,
    "When a booking, order or lab test needs a contact phone number, do NOT ask the caller to recite their number from scratch. Read THIS number back to them one digit at a time and ask whether to use it for the booking and the confirmation SMS, or whether they want to give a different number.",
    "Only if the caller wants a DIFFERENT number, ask for it one digit at a time and read it back. Always confirm the final number digit by digit — never grouped — before booking.",
  ].join("\n");
}

export function createGeminiLiveSession(
  cfg: AgentConfig,
  callerId?: string,
  retryMode = false,
  callerNum?: string,
  extraContext?: string
): Promise<GeminiLiveSession> {
  return new Promise((resolve, reject) => {
    const wsUrl = getWsUrl();
    const ws = new WebSocket(wsUrl);

    let audioCallback: ((audioBase64: string) => void) | null = null;
    let transcriptCallback: ((text: string, role: string) => void) | null = null;
    let interruptCallback: (() => void) | null = null;
    let turnCompleteCallback: (() => void) | null = null;
    let toolCallback: ((call: ToolCallEvent) => void) | null = null;
    let errorCallback: ((error: string) => void) | null = null;
    let closeCallback: (() => void) | null = null;
    let isConfigured = false;

    ws.on("open", () => {
      console.log(
        `[Gemini] WebSocket connected (voice=${cfg.voice}, model=${cfg.model}, retry=${retryMode})`
      );

      let memoryContext: string | undefined;
      if (callerId) {
        try {
          const memory = loadMemory(callerId);
          const m = buildMemoryContext(memory);
          if (m) memoryContext = m;
        } catch (_) {}
      }

      let systemText = buildSystemText(cfg, memoryContext);
      const callerNumBlock = callerNumberContext(callerNum);
      if (callerNumBlock) systemText += "\n\n" + callerNumBlock;
      // Per-call context resolved by the bridge before the session opens — e.g.
      // the restaurant CRM hit that lets the agent greet a regular by name.
      if (extraContext && extraContext.trim()) systemText += "\n\n" + extraContext.trim();

      const configMessage = {
        setup: {
          model: `models/${cfg.model}`,
          generationConfig: {
            responseModalities: ["AUDIO"],
            speechConfig: {
              voiceConfig: {
                prebuiltVoiceConfig: { voiceName: cfg.voice },
              },
            },
          },
          systemInstruction: { parts: [{ text: systemText }] },
          tools: buildTools(cfg),
          outputAudioTranscription: {},
          inputAudioTranscription: {},
        },
      };

      ws.send(JSON.stringify(configMessage));
    });

    ws.on("message", (data: WebSocket.Data) => {
      let response: any;
      try {
        response = JSON.parse(data.toString());
      } catch (err) {
        console.error("Failed to parse Gemini response:", err);
        return;
      }

      if (response.setupComplete && !isConfigured) {
        isConfigured = true;
        resolve(session);
        return;
      }

      if (response.serverContent?.modelTurn?.parts) {
        for (const part of response.serverContent.modelTurn.parts) {
          if (part.inlineData?.data) audioCallback?.(part.inlineData.data);
          if (part.text) transcriptCallback?.(part.text, "agent");
        }
      }

      if (response.serverContent?.outputTranscription?.text) {
        transcriptCallback?.(
          response.serverContent.outputTranscription.text,
          "agent"
        );
      }

      if (response.serverContent?.inputTranscription?.text) {
        transcriptCallback?.(
          response.serverContent.inputTranscription.text,
          "user"
        );
      }

      if (response.serverContent?.interrupted) interruptCallback?.();
      if (response.serverContent?.turnComplete) turnCompleteCallback?.();

      if (response.toolCall?.functionCalls) {
        for (const fc of response.toolCall.functionCalls) {
          console.log(
            `[Gemini] toolCall: ${fc.name}(${JSON.stringify(fc.args)})`
          );
          toolCallback?.({
            name: fc.name,
            args: fc.args || {},
            id: fc.id || fc.name,
          });
        }
      }
    });

    ws.on("error", (err) => {
      console.error("Gemini WS error:", err.message);
      errorCallback?.(err.message);
      if (!isConfigured) reject(err);
    });

    ws.on("close", (code, reason) => {
      console.log(
        "[Gemini] WS closed. Code:", code, "Reason:", reason?.toString()
      );
      closeCallback?.();
    });

    const session: GeminiLiveSession = {
      ws,
      send: (audioBase64: string) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            realtimeInput: {
              audio: { data: audioBase64, mimeType: "audio/pcm;rate=16000" },
            },
          }));
        }
      },
      sendText: (text: string) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ realtimeInput: { text } }));
        }
      },
      sendToolResponse: (id: string, name: string, result: unknown) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            toolResponse: {
              functionResponses: [{ id, name, response: { result } }],
            },
          }));
        }
      },
      close: () => {
        if (ws.readyState === WebSocket.OPEN) ws.close();
      },
      onAudio: (cb) => { audioCallback = cb; },
      onTranscript: (cb) => { transcriptCallback = cb; },
      onInterrupt: (cb) => { interruptCallback = cb; },
      onTurnComplete: (cb) => { turnCompleteCallback = cb; },
      onToolCall: (cb) => { toolCallback = cb; },
      onError: (cb) => { errorCallback = cb; },
      onClose: (cb) => { closeCallback = cb; },
    };

    setTimeout(() => {
      if (!isConfigured) {
        ws.close();
        reject(new Error("Gemini Live API connection timeout"));
      }
    }, 10000);
  });
}
