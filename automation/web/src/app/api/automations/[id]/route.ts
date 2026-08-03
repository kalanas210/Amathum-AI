import {
  deleteWorkflow,
  loadWorkflow,
  updateWorkflow,
  type WorkflowPatch,
} from "@/lib/engine";
import { badRequest, json, notFound } from "@/lib/http";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ id: string }> };

// GET /api/automations/:id -> load a workflow
export async function GET(_req: Request, { params }: Ctx) {
  const { id } = await params;
  const wf = await loadWorkflow(id);
  return wf ? json(wf) : notFound();
}

// PUT /api/automations/:id { nodes, connections, ... } -> save (validates, snapshots)
export async function PUT(request: Request, { params }: Ctx) {
  const { id } = await params;
  const body = (await request.json().catch(() => ({}))) as WorkflowPatch;
  const result = await updateWorkflow(id, body);
  if (result.ok) return json(result.wf);
  if (result.notFound) return notFound();
  return badRequest(result.error || "invalid workflow");
}

// DELETE /api/automations/:id
export async function DELETE(_req: Request, { params }: Ctx) {
  const { id } = await params;
  await deleteWorkflow(id);
  return json({ ok: true });
}
