import { NODE_CATALOG } from "@/lib/engine";
import { json } from "@/lib/http";

// GET /api/automations/node-catalog -> node types + config fields (static)
export async function GET() {
  return json({ nodes: NODE_CATALOG });
}
