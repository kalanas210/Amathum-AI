import { importWorkflow } from "@/lib/engine";
import { badRequest, json } from "@/lib/http";

export const dynamic = "force-dynamic";

// POST /api/automations/import { name, nodes, connections } -> create from JSON
export async function POST(request: Request) {
  const body = await request.json().catch(() => null);
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return badRequest("invalid JSON");
  }
  const result = await importWorkflow(body as Record<string, unknown>);
  if (!result.ok) return badRequest(result.error || "invalid workflow");
  return json(result.wf, 201);
}
