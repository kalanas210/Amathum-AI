import { type Item, loadWorkflow, runWorkflow, saveRun } from "@/lib/engine";
import { json, notFound } from "@/lib/http";

export const dynamic = "force-dynamic";

// POST /api/automations/:id/run { payload? } -> run now (manual); returns run log
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const wf = await loadWorkflow(id);
  if (!wf) return notFound();

  const body = (await request.json().catch(() => ({}))) as { payload?: Item };
  const run = await runWorkflow(wf, {
    triggerPayload: body.payload ?? null,
    triggerKind: "manual",
  });
  await saveRun(id, run);
  return json(run);
}
