import { exportWorkflow } from "@/lib/engine";
import { notFound } from "@/lib/http";

export const dynamic = "force-dynamic";

// GET /api/automations/:id/export -> downloadable workflow JSON
export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const clean = await exportWorkflow(id);
  if (!clean) return notFound();

  const fname = `${(clean.id as string) || "workflow"}.json`;
  return new Response(JSON.stringify(clean, null, 2), {
    headers: {
      "Content-Type": "application/json",
      "Content-Disposition": `attachment; filename="${fname}"`,
    },
  });
}
