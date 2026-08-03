// Conversion between the workflow JSON model and the React Flow graph.

import type { Edge, Node } from "@xyflow/react";

import type {
  Connections,
  NodeCatalogEntry,
  NodeGroup,
  NodeRunStatus,
  Workflow,
  WorkflowNode,
} from "@/lib/engine/types";

export interface AutomationNodeData {
  label: string;
  nodeType: string; // catalog type, e.g. "if"
  typeName: string; // catalog display name
  description: string;
  group: NodeGroup;
  icon: string;
  outputs: number;
  parameters: Record<string, unknown>;
  status?: NodeRunStatus | "";
  [key: string]: unknown; // React Flow requires node data to be a record
}

export type AutomationNode = Node<AutomationNodeData, "automation">;

export function nodeData(
  cat: NodeCatalogEntry | undefined,
  type: string,
  label: string,
  parameters: Record<string, unknown>,
): AutomationNodeData {
  return {
    label,
    nodeType: type,
    typeName: cat?.name ?? type,
    description: cat?.description ?? "",
    group: cat?.group ?? "action",
    icon: cat?.icon ?? "box",
    outputs: cat?.outputs ?? 1,
    parameters,
    status: "",
  };
}

export function defaultParams(cat: NodeCatalogEntry | undefined): Record<string, unknown> {
  const p: Record<string, unknown> = {};
  for (const f of cat?.params ?? []) {
    p[f.key] = structuredClone(f.default ?? null);
  }
  return p;
}

export function workflowToFlow(
  wf: Workflow,
  catalogByType: Record<string, NodeCatalogEntry>,
): { nodes: AutomationNode[]; edges: Edge[] } {
  const nodes: AutomationNode[] = (wf.nodes ?? []).map((n) => ({
    id: n.id,
    type: "automation",
    position: n.position ?? { x: 0, y: 0 },
    data: nodeData(catalogByType[n.type], n.type, n.name || n.type, n.parameters ?? {}),
  }));

  const edges: Edge[] = [];
  for (const [src, conn] of Object.entries(wf.connections ?? {})) {
    (conn?.main ?? []).forEach((targets, oi) => {
      (targets ?? []).forEach((t, ti) => {
        if (t?.node) {
          edges.push({
            id: `${src}:${oi}:${t.node}:${ti}`,
            source: src,
            target: t.node,
            sourceHandle: String(oi),
          });
        }
      });
    });
  }
  return { nodes, edges };
}

export function flowToWorkflow(
  nodes: AutomationNode[],
  edges: Edge[],
): { nodes: WorkflowNode[]; connections: Connections } {
  const wfNodes: WorkflowNode[] = nodes.map((n) => ({
    id: n.id,
    name: n.data.label,
    type: n.data.nodeType,
    position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    parameters: n.data.parameters ?? {},
  }));

  const connections: Connections = {};
  for (const e of edges) {
    const oi = Number.parseInt(e.sourceHandle ?? "0", 10) || 0;
    const c = (connections[e.source] ??= { main: [] });
    while (c.main.length <= oi) c.main.push([]);
    c.main[oi].push({ node: e.target, index: 0 });
  }
  return { nodes: wfNodes, connections };
}
