import { loadVersion } from "@/lib/engine";
import { json, notFound } from "@/lib/http";

export const dynamic = "force-dynamic";

// GET /api/automations/:id/versions/:v -> load a saved version
export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string; v: string }> },
) {
  const { id, v } = await params;
  const snap = await loadVersion(id, Number.parseInt(v, 10));
  return snap ? json(snap) : notFound();
}
