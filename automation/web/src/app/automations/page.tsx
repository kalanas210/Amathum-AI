import { WorkflowDashboard } from "@/components/automations/workflow-dashboard";
import { listWorkflowSummaries } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function AutomationsPage() {
  const workflows = await listWorkflowSummaries();
  return <WorkflowDashboard initial={workflows} />;
}
