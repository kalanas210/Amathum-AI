// PUBLIC trigger endpoint — no login gate; protected by the unguessable token.
// Serves two trigger types that share the token mechanism:
//   - webhookTrigger        generic webhook ({body, query, headers} item)
//   - callCompletedTrigger  AI call-completed event (payload IS the item)
import { findByToken, type Item, runWorkflow, saveRun } from "@/lib/engine";

export const dynamic = "force-dynamic";

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, X-Webhook-Secret",
  "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
};

function jsonResponse(data: unknown, status = 200, extra: Record<string, string> = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, "Content-Type": "application/json", ...extra },
  });
}

async function handle(
  request: Request,
  { params }: { params: Promise<{ token: string }> },
): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS });
  }

  const { token } = await params;
  const found = await findByToken(token, ["webhookTrigger", "callCompletedTrigger"]);
  if (!found) return jsonResponse({ error: "unknown webhook" }, 404);

  const { wf, node } = found;
  if (!wf.active) return jsonResponse({ error: "workflow is inactive" }, 403);

  const p = (node.parameters || {}) as Record<string, unknown>;
  if (
    p.auth === "header secret" &&
    request.headers.get("X-Webhook-Secret") !== (p.secret || "")
  ) {
    return jsonResponse({ error: "bad or missing X-Webhook-Secret" }, 401);
  }

  // ---- AI call-completed trigger: the JSON payload IS the trigger item ----
  if (node.type === "callCompletedTrigger") {
    let payload: Record<string, unknown>;
    try {
      const parsed = JSON.parse(await request.text());
      payload = parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return jsonResponse({ error: "invalid JSON body" }, 400);
    }

    const call = payload.call as { direction?: string } | undefined;
    const agentId = (payload.agent as { id?: string } | undefined)?.id ?? "";

    const wantAgent = String(p.agent ?? "").trim();
    if (wantAgent && agentId !== wantAgent) {
      return jsonResponse({ ok: true, skipped: "agent" }, 202);
    }
    const wantDir = String(p.direction ?? "any");
    if (wantDir !== "any" && (call?.direction ?? "") !== wantDir) {
      return jsonResponse({ ok: true, skipped: "direction" }, 202);
    }

    const run = await runWorkflow(wf, {
      triggerPayload: payload as Item,
      triggerNodeId: node.id,
      triggerKind: "call",
    });
    await saveRun(wf.id, run);
    return jsonResponse({ ok: true, status: run.status });
  }

  // ---- generic webhook trigger ----
  const raw = await request.text().catch(() => "");
  let body: unknown = {};
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = Object.fromEntries(new URLSearchParams(raw).entries());
    }
  }
  const url = new URL(request.url);
  const item = {
    body,
    query: Object.fromEntries(url.searchParams.entries()),
    headers: Object.fromEntries(request.headers.entries()),
  };

  const run = await runWorkflow(wf, {
    triggerPayload: item,
    triggerNodeId: node.id,
    triggerKind: "webhook",
  });
  await saveRun(wf.id, run);

  const resp = run.response;
  if (p.responseMode === "usingRespondNode" && resp) {
    if (resp.bodyType === "json") {
      let data: unknown;
      try {
        data = resp.body && resp.body.trim() ? JSON.parse(resp.body) : {};
      } catch {
        data = { raw: resp.body };
      }
      return jsonResponse(data, resp.statusCode);
    }
    return new Response(resp.body, {
      status: resp.statusCode,
      headers: { ...CORS, "Content-Type": "text/plain" },
    });
  }
  return jsonResponse({ ok: true, status: run.status });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
