import { setActive } from "@/lib/engine";
import { json, notFound } from "@/lib/http";

export const dynamic = "force-dynamic";

// POST /api/automations/:id/activate { active? } -> toggle / set the active flag
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const body = (await request.json().catch(() => ({}))) as { active?: boolean };
  const result = await setActive(id, body.active);
  return result.ok ? json(result.wf) : notFound();
}
