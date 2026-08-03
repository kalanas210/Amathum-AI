import { listVersions } from "@/lib/engine";
import { json } from "@/lib/http";

export const dynamic = "force-dynamic";

// GET /api/automations/:id/versions -> version history (metadata)
export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return json({ versions: await listVersions(id) });
}
