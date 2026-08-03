import { createWorkflow, listWorkflowSummaries } from "@/lib/engine";
import { json } from "@/lib/http";

export const dynamic = "force-dynamic";

// GET /api/automations -> list workflows + run stats
export async function GET() {
  return json({ workflows: await listWorkflowSummaries() });
}

// POST /api/automations { name } -> create
export async function POST(request: Request) {
  const body = (await request.json().catch(() => ({}))) as { name?: string };
  const wf = await createWorkflow(body.name || "Untitled workflow");
  return json(wf, 201);
}
