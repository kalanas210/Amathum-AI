"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  BackgroundVariant,
  Controls,
  type Edge,
  type EdgeChange,
  type NodeChange,
  type NodeTypes,
  type OnConnect,
  ReactFlow,
} from "@xyflow/react";
import { useTheme } from "next-themes";

import type { AutomationNode } from "./flow";
import { NodeCard } from "./node-card";

const nodeTypes: NodeTypes = { automation: NodeCard };

interface Props {
  nodes: AutomationNode[];
  edges: Edge[];
  onNodesChange: (changes: NodeChange<AutomationNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: OnConnect;
  onSelectNode: (id: string | null) => void;
}

export function FlowCanvas({
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onSelectNode,
}: Props) {
  const { resolvedTheme } = useTheme();

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onNodeClick={(_, node) => onSelectNode(node.id)}
      onPaneClick={() => onSelectNode(null)}
      nodeTypes={nodeTypes}
      colorMode={resolvedTheme === "dark" ? "dark" : "light"}
      fitView
      fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
      defaultEdgeOptions={{ type: "smoothstep" }}
      deleteKeyCode={["Backspace", "Delete"]}
      minZoom={0.2}
      maxZoom={1.8}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1.5} />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
