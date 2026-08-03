"use client";

import {
  addEdge,
  type Connection,
  type Edge,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import { Plus } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { NodeCatalogEntry, RunLog, Workflow } from "@/lib/engine/types";

import { BuilderProvider } from "./builder-context";
import { ConfigPanel } from "./config-panel";
import {
  type AutomationNode,
  defaultParams,
  flowToWorkflow,
  nodeData,
  workflowToFlow,
} from "./flow";
import { FlowCanvas } from "./flow-canvas";
import { NodePicker } from "./node-picker";
import { RunLogPanel } from "./run-log";
import { TopBar } from "./top-bar";
import { VersionHistory } from "./version-history";

interface BuilderProps {
  workflow: Workflow;
  catalog: NodeCatalogEntry[];
}

export function Builder(props: BuilderProps) {
  return (
    <ReactFlowProvider>
      <BuilderInner {...props} />
    </ReactFlowProvider>
  );
}

function BuilderInner({ workflow, catalog }: BuilderProps) {
  const catalogByType = useMemo(
    () =>
      Object.fromEntries(catalog.map((c) => [c.type, c])) as Record<
        string,
        NodeCatalogEntry
      >,
    [catalog],
  );
  const initial = useMemo(
    () => workflowToFlow(workflow, catalogByType),
    [workflow, catalogByType],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState<AutomationNode>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);
  const [name, setName] = useState(workflow.name);
  const [active, setActive] = useState(workflow.active);
  const [version, setVersion] = useState(workflow.version);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<RunLog | null>(null);
  const [runLogOpen, setRunLogOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [picker, setPicker] = useState<{
    open: boolean;
    source?: { id: string; output: number };
  }>({ open: false });

  const { screenToFlowPosition } = useReactFlow();

  const handleNodesChange = useCallback(
    (changes: Parameters<typeof onNodesChange>[0]) => {
      onNodesChange(changes);
      if (changes.some((c) => c.type !== "select" && c.type !== "dimensions")) {
        setDirty(true);
      }
    },
    [onNodesChange],
  );

  const handleEdgesChange = useCallback(
    (changes: Parameters<typeof onEdgesChange>[0]) => {
      onEdgesChange(changes);
      if (changes.some((c) => c.type !== "select")) setDirty(true);
    },
    [onEdgesChange],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      setEdges((eds) => addEdge(c, eds));
      setDirty(true);
    },
    [setEdges],
  );

  const centerPos = useCallback(() => {
    try {
      const p = screenToFlowPosition({
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
      });
      return { x: p.x - 110, y: p.y - 28 };
    } catch {
      return { x: 220, y: 160 };
    }
  }, [screenToFlowPosition]);

  const addNode = useCallback(
    (cat: NodeCatalogEntry, source?: { id: string; output: number }) => {
      const id = `${cat.type}-${Math.random().toString(36).slice(2, 7)}`;
      let position = centerPos();
      if (source) {
        const src = nodes.find((n) => n.id === source.id);
        if (src) {
          position = {
            x: src.position.x + 300,
            y: src.position.y + (source.output === 1 ? 96 : 0),
          };
        }
      }
      const newNode: AutomationNode = {
        id,
        type: "automation",
        position,
        data: nodeData(cat, cat.type, cat.name, defaultParams(cat)),
      };
      setNodes((ns) => [...ns, newNode]);
      if (source) {
        setEdges((es) =>
          addEdge(
            {
              source: source.id,
              target: id,
              sourceHandle: String(source.output),
              targetHandle: null,
            },
            es,
          ),
        );
      }
      setDirty(true);
      setSelectedId(id);
    },
    [centerPos, nodes, setEdges, setNodes],
  );

  const onAddAfter = useCallback((id: string, output: number) => {
    setPicker({ open: true, source: { id, output } });
  }, []);

  const applyWorkflow = useCallback(
    (wf: Workflow) => {
      const f = workflowToFlow(wf, catalogByType);
      setNodes(f.nodes);
      setEdges(f.edges);
      setName(wf.name);
      setActive(wf.active);
      setVersion(wf.version);
      setSelectedId(null);
      setDirty(false);
    },
    [catalogByType, setEdges, setNodes],
  );

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const { nodes: wfNodes, connections } = flowToWorkflow(nodes, edges);
      const saved = await api.save(workflow.id, {
        name,
        active,
        nodes: wfNodes,
        connections,
      });
      // Merge back only server-side param changes (e.g. generated webhook/form
      // tokens) by node id — preserves node identity, positions and selection,
      // and never clobbers edits made while the save was in flight.
      const savedParams = new Map(saved.nodes.map((n) => [n.id, n.parameters]));
      setNodes((ns) =>
        ns.map((n) =>
          savedParams.has(n.id)
            ? {
                ...n,
                data: {
                  ...n.data,
                  parameters: savedParams.get(n.id) ?? n.data.parameters,
                },
              }
            : n,
        ),
      );
      setVersion(saved.version);
      setDirty(false);
      toast.success("Saved");
      return true;
    } catch (e) {
      toast.error((e as Error).message);
      return false;
    } finally {
      setSaving(false);
    }
  }, [active, edges, name, nodes, setNodes, workflow.id]);

  const handleRun = useCallback(async () => {
    if (dirty) {
      const ok = await handleSave();
      if (!ok) return;
    }
    setRunning(true);
    try {
      const run = await api.run(workflow.id);
      setLastRun(run);
      setRunLogOpen(true);
      const statusById = Object.fromEntries(
        run.node_runs.map((r) => [r.node_id, r.status]),
      );
      setNodes((ns) =>
        ns.map((n) => ({ ...n, data: { ...n.data, status: statusById[n.id] ?? "" } })),
      );
      if (run.status === "success") toast.success("Run succeeded");
      else toast.error(`Run failed${run.error ? `: ${run.error}` : ""}`);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [dirty, handleSave, setNodes, workflow.id]);

  const handleToggleActive = useCallback(
    async (v: boolean) => {
      setActive(v);
      try {
        await api.setActive(workflow.id, v);
        toast.success(v ? "Workflow activated" : "Workflow deactivated");
      } catch (e) {
        toast.error((e as Error).message);
        setActive(!v);
      }
    },
    [workflow.id],
  );

  const updateSelected = useCallback(
    (patch: { label?: string; parameters?: Record<string, unknown> }) => {
      if (!selectedId) return;
      setNodes((ns) =>
        ns.map((n) =>
          n.id === selectedId
            ? {
                ...n,
                data: {
                  ...n.data,
                  ...(patch.label !== undefined ? { label: patch.label } : {}),
                  ...(patch.parameters !== undefined
                    ? { parameters: patch.parameters }
                    : {}),
                },
              }
            : n,
        ),
      );
      setDirty(true);
    },
    [selectedId, setNodes],
  );

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    setNodes((ns) => ns.filter((n) => n.id !== selectedId));
    setEdges((es) => es.filter((e) => e.source !== selectedId && e.target !== selectedId));
    setSelectedId(null);
    setDirty(true);
  }, [selectedId, setEdges, setNodes]);

  const selected = nodes.find((n) => n.id === selectedId);

  return (
    <BuilderProvider value={{ catalog, catalogByType, onAddAfter }}>
      <div className="flex h-screen flex-col">
        <TopBar
          name={name}
          onNameChange={(v) => {
            setName(v);
            setDirty(true);
          }}
          active={active}
          onToggleActive={handleToggleActive}
          version={version}
          dirty={dirty}
          saving={saving}
          running={running}
          exportUrl={api.exportUrl(workflow.id)}
          onSave={handleSave}
          onRun={handleRun}
          onAddNode={() => setPicker({ open: true })}
          onOpenHistory={() => setHistoryOpen(true)}
          onOpenRunLog={() => setRunLogOpen(true)}
        />

        <div className="flex min-h-0 flex-1">
          <div className="relative min-w-0 flex-1">
            <FlowCanvas
              nodes={nodes}
              edges={edges}
              onNodesChange={handleNodesChange}
              onEdgesChange={handleEdgesChange}
              onConnect={onConnect}
              onSelectNode={setSelectedId}
            />
            {nodes.length === 0 && (
              <div className="pointer-events-none absolute inset-0 grid place-items-center">
                <Button
                  className="pointer-events-auto"
                  onClick={() => setPicker({ open: true })}
                >
                  <Plus className="size-4" />
                  Add your first node
                </Button>
              </div>
            )}
          </div>

          {selected && (
            <ConfigPanel
              key={selected.id}
              node={selected}
              catalogByType={catalogByType}
              lastRun={lastRun}
              onChange={updateSelected}
              onDelete={deleteSelected}
              onClose={() => setSelectedId(null)}
            />
          )}
        </div>
      </div>

      <NodePicker
        open={picker.open}
        onOpenChange={(o) => setPicker((p) => ({ ...p, open: o }))}
        catalog={catalog}
        hideTriggers={!!picker.source}
        onPick={(cat) => {
          addNode(cat, picker.source);
          setPicker({ open: false });
        }}
      />
      <RunLogPanel
        open={runLogOpen}
        onOpenChange={setRunLogOpen}
        run={lastRun}
        workflowId={workflow.id}
      />
      <VersionHistory
        open={historyOpen}
        onOpenChange={setHistoryOpen}
        workflowId={workflow.id}
        onRestored={applyWorkflow}
      />
    </BuilderProvider>
  );
}
