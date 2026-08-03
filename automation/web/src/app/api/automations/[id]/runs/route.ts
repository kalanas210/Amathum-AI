import { listRuns } from "@/lib/engine";
import { json } from "@/lib/http";

export const dynamic = "force-dynamic";

// GET /api/automations/:id/runs -> recent run logs
export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return json({ runs: await listRuns(id) });
}
