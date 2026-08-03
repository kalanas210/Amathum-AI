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
        phone: { type: "STRING", description: "Contact phone number. If the caller doesn't give one, use the number they are calling from." },
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
        phone: { type: "STRING", description: "Contact phone number. If not given, use the number they are calling from." },
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
        phone: { type: "STRING", description: "Contact phone number. If not given, use the number they're calling from." },
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
        phone: { type: "STRING", description: "Contact phone. If not given, use the number they are calling from." },
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

export function createGeminiLiveSession(
  cfg: AgentConfig,
  callerId?: string,
  retryMode = false
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

      const systemText = buildSystemText(cfg, memoryContext);

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
