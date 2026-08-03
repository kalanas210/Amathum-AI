import { restoreVersion } from "@/lib/engine";
import { json, notFound } from "@/lib/http";

export const dynamic = "force-dynamic";

// POST /api/automations/:id/restore/:v -> restore a version (snapshots current first)
export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string; v: string }> },
) {
  const { id, v } = await params;
  const result = await restoreVersion(id, Number.parseInt(v, 10));
  return result.ok ? json(result.wf) : notFound();
}
