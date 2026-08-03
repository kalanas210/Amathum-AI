# Wire sampath-ai → automations "Call Completed" node

The automations app now has a **Call Completed** trigger node. It receives a
`call.completed` JSON webhook and starts the workflow with the payload as
`$json` (so steps can use `{{ $json.transcript_text }}`, `{{ $json.outcome.booking_id }}`,
`{{ $json.agent.id }}`, etc.).

`bridge.ts` does **not** emit that webhook yet. This doc adds it. `bridge.ts` and
`.env` are owned by `asterisk`, so the edits below need `sudo`.

## 0. Get the node's webhook URL

In the builder: add a **Call Completed** node → **Save** → open the node → copy the
**Call webhook URL** shown in its panel. It looks like:

```
http://100.68.210.114:5056/api/automations/hook/<token>
```

(The server binds the Tailscale IP, so use `100.68.210.114`, not `localhost`. The
bridge runs on the same host and can reach it.) Activate the workflow (the **Active**
toggle) — inactive workflows return 403.

## 1. `.env` (sudo)

Append to `/opt/sampath-ai/.env`:

```bash
CALL_WEBHOOK_URL=http://100.68.210.114:5056/api/automations/hook/<token>
# optional shared secret — if set, also set "Auth: header secret" on the node and paste this value
CALL_WEBHOOK_SECRET=
```

## 2. `bridge.ts` edits (sudo)

### 2a. Read the env (near line 46, by `SESSIONS_DIR`)

```ts
const CALL_WEBHOOK_URL = process.env.CALL_WEBHOOK_URL || "";
const CALL_WEBHOOK_SECRET = process.env.CALL_WEBHOOK_SECRET || "";
```

### 2b. Record the agent identity on the `gemini_ready` event (line ~1008)

`session_open` happens before the flow is chosen, so stamp the agent on
`gemini_ready` (where `cfg` is known). Change:

```ts
appendSessionEvent(formattedUuid, {
  type: "gemini_ready",
  voice: cfg.voice,
  model: cfg.model,
});
```

to:

```ts
appendSessionEvent(formattedUuid, {
  type: "gemini_ready",
  voice: cfg.voice,
  model: cfg.model,
  agent_id: (cfg as any).id ?? null,     // e.g. "durdans" — the active flow id
  agent_name: (cfg as any).name ?? null,
});
```

### 2c. Add the emitter + payload builder (top-level, e.g. after `appendSessionEvent`, ~line 400)

The transcript/events are an append-only JSONL on disk; build the payload by
reading the (now complete) session file. `fetch`, `fs`, `path`, `SESSIONS_DIR`,
and `callLanguage` already exist in this file.

```ts
function buildCompletedPayload(state: BridgeState, formatted: string, reason: string) {
  const file = path.join(SESSIONS_DIR, `${formatted}.jsonl`);
  let events: any[] = [];
  try {
    events = fs
      .readFileSync(file, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => { try { return JSON.parse(l); } catch { return null; } })
      .filter(Boolean);
  } catch { /* file missing — emit minimal payload */ }

  const first = (t: string) => events.find((e) => e.type === t) || {};
  const open = first("session_open");
  const ready = first("gemini_ready");
  const closeEv = [...events].reverse().find((e) => e.type === "session_close") || {};

  const mode = open.mode || "primary";
  const startedAt = open.ts || null;
  const endedAt = closeEv.ts || new Date().toISOString();
  const durationSec =
    startedAt ? (new Date(endedAt).getTime() - new Date(startedAt).getTime()) / 1000 : null;

  // Group consecutive same-role transcript fragments into utterances.
  const transcript: { role: string; text: string; ts?: string }[] = [];
  let cur: any = null;
  for (const e of events) {
    if (e.type === "transcript" && e.text) {
      if (cur && cur.role === e.role && !cur._ended) {
        cur.text = `${cur.text} ${e.text}`.replace(/\s+/g, " ").trim();
      } else {
        cur = { role: e.role, text: e.text, ts: e.ts };
        transcript.push(cur);
      }
    } else if (e.type === "turn_complete" || e.type === "interrupted") {
      if (cur) cur._ended = true;
    }
  }
  const cleanTranscript = transcript.map(({ role, text, ts }) => ({ role, text, ts }));
  const transcriptText = cleanTranscript.map((u) => `${u.role}: ${u.text}`).join("\n");

  const toolCalls = events
    .filter((e) => e.type === "tool_call")
    .map((e) => ({ name: e.name, args: e.args }));

  const extracted: Record<string, unknown> = {};
  for (const e of events) if (e.type === "extracted" && e.field) extracted[e.field] = e.value;

  const outcomeEv = events.find((e) =>
    ["booking_created", "order_created", "order_confirmed", "reservation_created", "lab_ordered", "call_outcome"].includes(e.type),
  );
  const outcome = outcomeEv
    ? { type: outcomeEv.type, booking_id: outcomeEv.booking_id ?? outcomeEv.ref ?? null, ...outcomeEv }
    : null;

  return {
    event: "call.completed",
    ts: new Date().toISOString(),
    call: {
      uuid: formatted,
      mode,
      direction: mode === "outbound" ? "outbound" : "inbound",
      channel: open.channel ?? null,
      caller_num: open.caller_num ?? null,
      started_at: startedAt,
      ended_at: endedAt,
      duration_sec: durationSec,
      close_reason: reason,
      escalated: !!state.escalating,
      language: callLanguage(state.langCounts),
    },
    agent: { id: ready.agent_id ?? null, name: ready.agent_name ?? null },
    transcript: cleanTranscript,
    transcript_text: transcriptText,
    tool_calls: toolCalls,
    extracted_fields: extracted,
    outcome,
  };
}

function emitCallCompleted(payload: Record<string, unknown>) {
  if (!CALL_WEBHOOK_URL) return;
  fetch(CALL_WEBHOOK_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(CALL_WEBHOOK_SECRET ? { "x-webhook-secret": CALL_WEBHOOK_SECRET } : {}),
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(5000),
  }).catch((e) => console.warn("[webhook] call.completed POST failed:", (e as Error).message));
}
```

### 2d. Fire it once in `close()` — right after `session_close` (line 1712)

`close()` is idempotent (the `state.closed` guard), so this fires exactly once per
call. Insert immediately after the `appendSessionEvent(formatted, { type: "session_close", reason });`
line, before the `fs.unlinkSync(...)` cleanup (the JSONL is complete and not deleted):

```ts
    appendSessionEvent(formatted, { type: "session_close", reason });
    try { emitCallCompleted(buildCompletedPayload(state, formatted, reason)); } catch (_) {}
```

## 3. Apply & restart (sudo)

```bash
sudo cp /opt/sampath-ai/bridge.ts /opt/sampath-ai/bridge.ts.bak-$(date +%s)   # match the existing backup habit
sudo nano /opt/sampath-ai/bridge.ts        # apply 2a–2d
sudo nano /opt/sampath-ai/.env             # add CALL_WEBHOOK_URL (step 1)
sudo systemctl restart sampath-ai
systemctl is-active sampath-ai             # -> active
journalctl -u sampath-ai -f                # watch; on the next completed call you'll see the POST (or a [webhook] warning)
```

Then place/receive a test call. The workflow's run log should show the call run with
the transcript and outcome.

## Notes / decisions

- **Agent filter:** the node's "Agent / flow id" matches `payload.agent.id` (e.g. `durdans`).
  Leave it blank to fire for every agent. With one active inbound flow, blank is usually fine.
- **Auth:** if you set `CALL_WEBHOOK_SECRET`, also set the node's `Auth` to `header secret`
  and paste the same value — the node then rejects requests without a matching `x-webhook-secret`.
- **PII:** the payload includes the full transcript and caller number. It only travels to the
  automations app over Tailscale. If you'd rather not POST full transcripts, send `call.uuid`
  only and have a workflow step fetch the session — tell me and I'll adjust.
- **No summary:** the bridge generates no call summary today; add an OpenAI/Code step in the
  workflow if you want one.
