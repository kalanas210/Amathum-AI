import { notFound } from "next/navigation";

import { Builder } from "@/components/automations/builder/builder";
import { loadWorkflow, NODE_CATALOG } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function BuilderPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const workflow = await loadWorkflow(id);
  if (!workflow) notFound();

  return <Builder workflow={workflow} catalog={NODE_CATALOG} />;
}
